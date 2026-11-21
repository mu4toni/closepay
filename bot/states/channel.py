from aiogram.fsm.state import State, StatesGroup


class ConnectChannelStates(StatesGroup):
    """FSM states for connecting a private channel or chat."""

    waiting_for_channel = State()


class CreateTariffStates(StatesGroup):
    """FSM states for creating a new tariff plan for a channel."""

    waiting_for_name = State()
    waiting_for_price = State()
    waiting_for_duration = State()


class EditChannelStates(StatesGroup):
    """FSM states for editing channel description."""

    waiting_for_description = State()
