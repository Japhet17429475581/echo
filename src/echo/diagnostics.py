"""Log failure codes without exception messages, SQL, paths or credentials."""


def failure(logger, event, stage, error):
    number = getattr(error, "errno", None)
    sqlstate = getattr(getattr(error, "orig", error), "sqlstate", None)
    if (
        not isinstance(sqlstate, str)
        or len(sqlstate) != 5
        or not sqlstate.isascii()
        or not sqlstate.isalnum()
    ):
        sqlstate = "-"
    logger.warning(
        "%s stage=%s error=%s errno=%s sqlstate=%s",
        event,
        stage,
        type(error).__name__,
        number if isinstance(number, int) else "-",
        sqlstate,
    )
