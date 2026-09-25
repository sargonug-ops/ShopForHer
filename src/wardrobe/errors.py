class WardrobeError(Exception):
    """Base error for the wardrobe archive."""


class PinterestError(WardrobeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PinterestAuthError(PinterestError):
    """OAuth or token failure. The user usually needs to run `wardrobe auth` again."""
