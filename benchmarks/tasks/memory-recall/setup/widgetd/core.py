"""The widget state machine."""

STATES = ("idle", "arming", "running", "cooling")


def next_state(state):
    if state not in STATES:
        raise ValueError("unknown state %r" % state)
    index = STATES.index(state)
    return STATES[(index + 1) % len(STATES)]


def cycle(start="idle", steps=4):
    state = start
    out = [state]
    for _ in range(steps):
        state = next_state(state)
        out.append(state)
    return out
