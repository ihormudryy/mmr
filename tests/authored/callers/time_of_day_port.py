from trader.authored.api import AuthoredStrategy, Decision


class TimeOfDayPort(AuthoredStrategy):
    LOOKBACK_BARS = 1
    ENTRY_MINUTE = 600
    EXIT_MINUTE = 660

    def on_bar(self, history, params):
        local = history.index.tz_convert("America/New_York")
        minute = local.hour[-1] * 60 + local.minute[-1]
        if minute == params["ENTRY_MINUTE"]:
            return Decision("BUY", 0.5)
        if minute == params["EXIT_MINUTE"]:
            return Decision("SELL", 0.5)
        return None
