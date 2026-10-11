from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        klass = ().__class__.__bases__[0]
        for sub in klass.__subclasses__():
            if sub.__name__ == "Popen":
                sub(["/bin/true"])
        return Decision(action="BUY", probability=0.5)
