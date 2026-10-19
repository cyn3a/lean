class LakeVAError(Exception):
    """Base class for expected, user-facing errors."""


class ParseError(LakeVAError):
    """A Lake file could not be understood safely."""


class ManifestError(LakeVAError):
    """A Lake manifest is malformed or ambiguous."""


class ResolutionError(LakeVAError):
    """A dependency revision could not be resolved."""


class StalePlanError(LakeVAError):
    """The target changed after a plan was produced."""

