import asyncio
from typing import Any, Callable, List, Mapping, Sequence

from autogen_core import AgentRuntime, Component, ComponentModel
from pydantic import BaseModel

from ...base import ChatAgent, TerminationCondition
from ...messages import BaseAgentEvent, BaseChatMessage, HandoffMessage, MessageFactory
from ...state import SwarmManagerState
from ._base_group_chat import BaseGroupChat
from ._base_group_chat_manager import BaseGroupChatManager
from ._events import GroupChatTermination


class SwarmGroupChatManager(BaseGroupChatManager):
    """A group chat manager that selects the next speaker based on handoff message only."""

    def __init__(
        self,
        name: str,
        group_topic_type: str,
        output_topic_type: str,
        participant_topic_types: List[str],
        participant_names: List[str],
        participant_descriptions: List[str],
        output_message_queue: asyncio.Queue[BaseAgentEvent | BaseChatMessage | GroupChatTermination],
        termination_condition: TerminationCondition | None,
        max_turns: int | None,
        message_factory: MessageFactory,
        emit_team_events: bool,
    ) -> None:
        super().__init__(
            name,
            group_topic_type,
            output_topic_type,
            participant_topic_types,
            participant_names,
            participant_descriptions,
            output_message_queue,
            termination_condition,
            max_turns,
            message_factory,
            emit_team_events,
        )
        self._current_speaker = self._participant_names[0]

    async def validate_group_state(self, messages: List[BaseChatMessage] | None) -> None:
        """Validate the start messages for the group chat."""
        # Check if any of the start messages is a handoff message.
        if messages:
            for message in messages:
                if isinstance(message, HandoffMessage):
                    if message.target not in self._participant_names:
                        raise ValueError(
                            f"The target {message.target} is not one of the participants {self._participant_names}. "
                            "If you are resuming Swarm with a new HandoffMessage make sure to set the target to a valid participant as the target."
                        )
                    return

        # Check if there is a handoff message in the thread that is not targeting a valid participant.
        for existing_message in reversed(self._message_thread):
            if isinstance(existing_message, HandoffMessage):
                if existing_message.target not in self._participant_names:
                    raise ValueError(
                        f"The existing handoff target {existing_message.target} is not one of the participants {self._participant_names}. "
                        "If you are resuming Swarm with a new task make sure to include in your task "
                        "a HandoffMessage with a valid participant as the target. For example, if you are "
                        "resuming from a HandoffTermination, make sure the new task is a HandoffMessage "
                        "with a valid participant as the target."
                    )
                # The latest handoff message should always target a valid participant.
                # Do not look past the latest handoff message.
                return

    async def reset(self) -> None:
        self._current_turn = 0
        self._message_thread.clear()
        if self._termination_condition is not None:
            await self._termination_condition.reset()
        self._current_speaker = self._participant_names[0]

    async def select_speaker(self, thread: Sequence[BaseAgentEvent | BaseChatMessage]) -> List[str] | str:
        """Select a speaker from the participants based on handoff message.
        Looks for the last handoff message in the thread to determine the next speaker.

        .. note::

            This method always returns a single speaker.
        """
        if len(thread) == 0:
            return [self._current_speaker]
        for message in reversed(thread):
            if isinstance(message, HandoffMessage):
                self._current_speaker = message.target
                # The latest handoff message should always target a valid participant.
                assert self._current_speaker in self._participant_names
                return [self._current_speaker]
        return self._current_speaker

    async def save_state(self) -> Mapping[str, Any]:
        state = SwarmManagerState(
            message_thread=[msg.dump() for msg in self._message_thread],
            current_turn=self._current_turn,
            current_speaker=self._current_speaker,
        )
        return state.model_dump()

    async def load_state(self, state: Mapping[str, Any]) -> None:
        swarm_state = SwarmManagerState.model_validate(state)
        self._message_thread = [self._message_factory.create(message) for message in swarm_state.message_thread]
        self._current_turn = swarm_state.current_turn
        self._current_speaker = swarm_state.current_speaker


class SwarmConfig(BaseModel):
    """The declarative configuration for Swarm."""

    name: str | None = None
    description: str | None = None
    participants: List[ComponentModel]
    termination_condition: ComponentModel | None = None
    max_turns: int | None = None
    emit_team_events: bool = False


class Swarm(BaseGroupChat, Component[SwarmConfig]):
    """A group chat team that selects the next speaker based on handoff message only.

    The first participant in the list of participants is the initial speaker.
    The next speaker is selected based on the :class:`~autogen_agentchat.messages.HandoffMessage` message
    sent by the current speaker. If no handoff message is sent, the current speaker
    continues to be the speaker.

    .. note::

        Unlike :class:`~autogen_agentchat.teams.RoundRobinGroupChat` and
        :class:`~autogen_agentchat.teams.SelectorGroupChat`, this group chat
        team does not support inner teams as participants.

    Args:
        participants (List[ChatAgent]): The agents participating in the group chat. The first agent in the list is the initial speaker.
        name (str | None, optional): The name of the group chat, using :attr:`~autogen_agentchat.teams.Swarm.DEFAULT_NAME` if not provided.
            The name is used by a parent team to identify this group chat so it must be unique within the parent team.
        description (str | None, optional): The description of the group chat, using :attr:`~autogen_agentchat.teams.Swarm.DEFAULT_DESCRIPTION` if not provided.
        termination_condition (TerminationCondition, optional): The termination condition for the group chat. Defaults to None.
            Without a termination condition, the group chat will run indefinitely.
        max_turns (int, optional): The maximum number of turns in the group chat before stopping. Defaults to None, meaning no limit.
        custom_message_types (List[type[BaseAgentEvent | BaseChatMessage]], optional): A list of custom message types that will be used in the group chat.
            If you are using custom message types or your agents produces custom message types, you need to specify them here.
            Make sure your custom message types are subclasses of :class:`~autogen_agentchat.messages.BaseAgentEvent` or :class:`~autogen_agentchat.messages.BaseChatMessage`.
        emit_team_events (bool, optional): Whether to emit team events through :meth:`BaseGroupChat.run_stream`. Defaults to False.

    Basic example:

        .. code-block:: python

            import asyncio
            from autogen_ext.models.openai import OpenAIChatCompletionClient
            from autogen_agentchat.agents import AssistantAgent
            from autogen_agentchat.teams import Swarm
            from autogen_agentchat.conditions import MaxMessageTermination


            async def main() -> None:
                model_client = OpenAIChatCompletionClient(model="gpt-4o")

                agent1 = AssistantAgent(
                    "Alice",
                    model_client=model_client,
                    handoffs=["Bob"],
                    system_message="You are Alice and you only answer questions about yourself.",
                )
                agent2 = AssistantAgent(
                    "Bob", model_client=model_client, system_message="You are Bob and your birthday is on 1st January."
                )

                termination = MaxMessageTermination(3)
                team = Swarm([agent1, agent2], termination_condition=termination)

                stream = team.run_stream(task="What is bob's birthday?")
                async for message in stream:
                    print(message)


            asyncio.run(main())


    Using the :class:`~autogen_agentchat.conditions.HandoffTermination` for human-in-the-loop handoff:

        .. code-block:: python

            import asyncio
            from autogen_ext.models.openai import OpenAIChatCompletionClient
            from autogen_agentchat.agents import AssistantAgent
            from autogen_agentchat.teams import Swarm
            from autogen_agentchat.conditions import HandoffTermination, MaxMessageTermination
            from autogen_agentchat.ui import Console
            from autogen_agentchat.messages import HandoffMessage


            async def main() -> None:
                model_client = OpenAIChatCompletionClient(model="gpt-4o")

                agent = AssistantAgent(
                    "Alice",
                    model_client=model_client,
                    handoffs=["user"],
                    system_message="You are Alice and you only answer questions about yourself, ask the user for help if needed.",
                )
                termination = HandoffTermination(target="user") | MaxMessageTermination(3)
                team = Swarm([agent], termination_condition=termination)

                # Start the conversation.
                await Console(team.run_stream(task="What is bob's birthday?"))

                # Resume with user feedback.
                await Console(
                    team.run_stream(
                        task=HandoffMessage(source="user", target="Alice", content="Bob's birthday is on 1st January.")
                    )
                )


            asyncio.run(main())
    """

    component_config_schema = SwarmConfig
    component_provider_override = "autogen_agentchat.teams.Swarm"

    DEFAULT_NAME = "Swarm"
    DEFAULT_DESCRIPTION = "A team of agents."

    def __init__(
        self,
        participants: List[ChatAgent],
        *,
        name: str | None = None,
        description: str | None = None,
        termination_condition: TerminationCondition | None = None,
        max_turns: int | None = None,
        runtime: AgentRuntime | None = None,
        custom_message_types: List[type[BaseAgentEvent | BaseChatMessage]] | None = None,
        emit_team_events: bool = False,
    ) -> None:
        for participant in participants:
            if not isinstance(participant, ChatAgent):
                raise TypeError(f"Participant {participant} must be a ChatAgent.")
        super().__init__(
            name=name or self.DEFAULT_NAME,
            description=description or self.DEFAULT_DESCRIPTION,
            participants=[participant for participant in participants],
            group_chat_manager_name="SwarmGroupChatManager",
            group_chat_manager_class=SwarmGroupChatManager,
            termination_condition=termination_condition,
            max_turns=max_turns,
            runtime=runtime,
            custom_message_types=custom_message_types,
            emit_team_events=emit_team_events,
        )
        # The first participant must be able to produce handoff messages.
        first_participant = self._participants[0]
        assert isinstance(first_participant, ChatAgent)
        if HandoffMessage not in first_participant.produced_message_types:
            raise ValueError("The first participant must be able to produce a handoff messages.")

    def _create_group_chat_manager_factory(
        self,
        name: str,
        group_topic_type: str,
        output_topic_type: str,
        participant_topic_types: List[str],
        participant_names: List[str],
        participant_descriptions: List[str],
        output_message_queue: asyncio.Queue[BaseAgentEvent | BaseChatMessage | GroupChatTermination],
        termination_condition: TerminationCondition | None,
        max_turns: int | None,
        message_factory: MessageFactory,
    ) -> Callable[[], SwarmGroupChatManager]:
        def _factory() -> SwarmGroupChatManager:
            return SwarmGroupChatManager(
                name,
                group_topic_type,
                output_topic_type,
                participant_topic_types,
                participant_names,
                participant_descriptions,
                output_message_queue,
                termination_condition,
                max_turns,
                message_factory,
                self._emit_team_events,
            )

        return _factory

    def _to_config(self) -> SwarmConfig:
        participants = [participant.dump_component() for participant in self._participants]
        termination_condition = self._termination_condition.dump_component() if self._termination_condition else None
        return SwarmConfig(
            name=self._name,
            description=self._description,
            participants=participants,
            termination_condition=termination_condition,
            max_turns=self._max_turns,
            emit_team_events=self._emit_team_events,
        )

    @classmethod
    def _from_config(cls, config: SwarmConfig) -> "Swarm":
        participants = [ChatAgent.load_component(participant) for participant in config.participants]
        termination_condition = (
            TerminationCondition.load_component(config.termination_condition) if config.termination_condition else None
        )
        return cls(
            participants,
            name=config.name,
            description=config.description,
            termination_condition=termination_condition,
            max_turns=config.max_turns,
            emit_team_events=config.emit_team_events,
        )
