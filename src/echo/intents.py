import re

from echo.ai import Intent, IntentName
from echo.domain import Category


def local_intent(text):
    value = text.strip().lower().rstrip(".!?")
    if value in {"покажи игровые сайты", "игровые сайты", "show gaming sites", "show game sites"}:
        return Intent(intent=IntentName.SET_CATEGORY_FILTER, category=Category.GAMES)
    if value in {"только ошибки", "покажи ошибки", "only errors", "show errors"}:
        return Intent(intent=IntentName.SHOW_ERRORS)
    if value in {"остановить поиск", "останови поиск", "stop search", "cancel search"}:
        return Intent(intent=IntentName.CANCEL_JOB)
    if value in {"повторить неудачные проверки", "повторить ошибки", "retry failed checks"}:
        return Intent(intent=IntentName.RETRY_FAILED)
    if value in {"покажи результаты", "show results"}:
        return Intent(intent=IntentName.SHOW_RESULTS)
    if value in {"помощь", "help"}:
        return Intent(intent=IntentName.HELP)
    match = re.fullmatch(r"(?:скачать|скачай|download|export)\s+(pdf|html|csv|json)", value)
    if match:
        return Intent(intent=IntentName.EXPORT_REPORT, format=match[1])
    return None
