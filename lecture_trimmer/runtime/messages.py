"""Short, plain messages for a lecture that failed; technical details stay in local diagnostics."""


def public_message(exc) -> str:
    from lecture_trimmer.auth.access_control import AccessUnavailable
    if isinstance(exc, AccessUnavailable):
        return 'The PW access service could not be reached. Wait a moment and retry. Your saved work is safe.'
    if isinstance(exc, PermissionError):
        return 'Your access could not be verified. Sign in again with your approved account and retry.'
    from lecture_trimmer.media.transcriber import TranscriptionServiceError
    if isinstance(exc, TranscriptionServiceError):
        return exc.public_message
    import lecture_trimmer.analysis.classifier as classifier
    quota_error = getattr(classifier, "ProviderQuotaError", ())
    if isinstance(exc, quota_error):
        return exc.public_message
    from lecture_trimmer.integrations.pw_proxy import ProxyCallError
    if isinstance(exc, ProxyCallError):
        if exc.category == 'access':
            return "Your access could not be verified. Sign in again and retry."
        if exc.category in {'timeout', 'connection'}:
            return "The AI service could not be reached. Check your internet and retry; finished steps are saved."
        return "The AI service is unavailable. Please try again later."
    from lecture_trimmer.analysis.lesson_classifier import StudyReviewRequired
    if isinstance(exc, StudyReviewRequired):
        return "Some teaching sections need another review. Enable selected-frame review and retry. No new videos were exported."
    from lecture_trimmer.media.audio import AudioExtractionError
    if isinstance(exc, AudioExtractionError):
        return "We could not read this video's audio. Try another copy of the video."
    from desktop_runtime.desktop_update import UpdateError
    if isinstance(exc, UpdateError):
        return str(exc)
    public = getattr(exc, "public_message", None)
    if public:
        return public
    if isinstance(exc, ValueError) and 0 < len(str(exc)) < 300:
        return str(exc)
    return "We couldn't finish this lecture. Retry once, or send a support report to the tool owner."
