"""測試共用的假文字模型。"""


class FakeLLM:
    def __init__(self, *responses, exc=None):
        self.responses = list(responses)
        self.exc = exc
        self.calls = []

    def generate_json(self, model, system, prompt, schema, timeout=90, thinking=""):
        self.calls.append({"model": model, "system": system, "prompt": prompt, "schema": schema, "thinking": thinking})
        if self.exc:
            raise self.exc
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
