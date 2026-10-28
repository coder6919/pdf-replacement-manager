"""Exception hierarchy. `user_message` is what the UI shows; it never contains document text."""


class PRMError(Exception):
    code = "error"
    retryable = False

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.user_message = message
        self.detail = detail


class InsufficientSpaceError(PRMError):
    code = "disk_space"
    retryable = True


class FileLockedError(PRMError):
    code = "file_locked"
    retryable = True


class CorruptPdfError(PRMError):
    code = "corrupt_pdf"


class EncryptedPdfError(PRMError):
    code = "encrypted_pdf"


class SignedPdfError(PRMError):
    code = "signed_pdf"


class OperationError(PRMError):
    code = "operation_failed"


class ValidationError(PRMError):
    code = "validation_failed"


class IntegrityError(PRMError):
    code = "integrity"


class NeedsReview(PRMError):
    code = "needs_review"
