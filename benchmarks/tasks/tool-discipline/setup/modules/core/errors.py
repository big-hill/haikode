"""Shared exception types."""


class PipelineError(RuntimeError):
    pass


class AdapterError(PipelineError):
    pass


class ConfigError(PipelineError):
    pass
