"""Protocol failures shared by worker admission and server business callbacks."""


class ProtocolViolation(RuntimeError):
    pass
