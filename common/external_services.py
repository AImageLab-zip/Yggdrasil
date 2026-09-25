"""The kinds of real-time external service this platform can be configured to call.

Processing in Yggdrasil has three natures. Two of them already have a declarative home:
a cluster job is a ``ProcessingStep`` row, and browser processing is a bundle entry. The
third -- a service that has to answer while the clinician waits -- had none, so the one
instance of it (Live Whisper) was four ``settings`` constants and a hardcoded consumer,
and the next one would have been four more.

This module is the missing declaration. A :class:`ServiceKind` says what a *class* of
service needs -- which URL schemes are meaningful, whether it needs an API key or a model
name, and which tuning parameters it accepts with what ranges. A ``common.ExternalService``
row is one *instance* of a kind, editable in the Django admin. Callers get neither: they
ask :mod:`common.external_config` for a resolved service and receive a frozen value
object, or ``None``.

Modelled on ``common/export_catalog.py``: a small declarative class, instances collected
in a module-level catalog, consumed generically. Adding a kind is an entry here plus a
client -- no model change and no migration.

The point of the parameter schema is that an admin cannot break a service from a text
box. ``ExternalService.clean()`` validates against it, so a temperature of 7 or a typo'd
key is a form error on the change page rather than a 500 in a clinician's face.
"""


class Parameter:
    """One admin-tunable knob of a service kind."""

    KINDS = ("float", "int", "str", "bool")

    def __init__(self, name, label, kind="float", *, default=None, minimum=None,
                 maximum=None, choices=None, help=""):
        if kind not in self.KINDS:
            raise ValueError(f"{name}: unknown parameter kind {kind!r}")
        self.name = name
        self.label = label
        self.kind = kind
        self.default = default
        self.minimum = minimum
        self.maximum = maximum
        self.choices = tuple(choices) if choices else None
        self.help = help

    def coerce(self, value):
        """Return ``value`` as this parameter's type, or raise ``ValueError``.

        JSON round-trips make an int out of ``1.0`` and a string out of anything a form
        posts, so coerce rather than type-check -- but refuse a bool for a number, which
        Python would otherwise happily treat as 1.
        """
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                return value.lower() == "true"
            raise ValueError(f"{self.name} must be true or false")
        if isinstance(value, bool):
            raise ValueError(f"{self.name} must be a {self.kind}, not a boolean")
        if self.kind == "float":
            return float(value)
        if self.kind == "int":
            return int(value)
        return str(value)

    def validate(self, value):
        """Coerced value, or ``ValueError`` naming the parameter and the bound."""
        coerced = self.coerce(value)
        if self.choices is not None and coerced not in self.choices:
            allowed = ", ".join(str(choice) for choice in self.choices)
            raise ValueError(f"{self.name} must be one of: {allowed}")
        if self.minimum is not None and coerced < self.minimum:
            raise ValueError(f"{self.name} must be at least {self.minimum}")
        if self.maximum is not None and coerced > self.maximum:
            raise ValueError(f"{self.name} must be at most {self.maximum}")
        return coerced


class ServiceKind:
    """A class of external service: what it needs, and what may be tuned on it."""

    def __init__(self, key, label, *, transport, allowed_schemes,
                 requires_api_key=True, requires_model=False,
                 parameter_schema=(), description=""):
        self.key = key
        self.label = label
        self.transport = transport                      # 'websocket' | 'http'
        self.allowed_schemes = tuple(allowed_schemes)
        self.requires_api_key = requires_api_key
        self.requires_model = requires_model
        self.parameters = {p.name: p for p in parameter_schema}
        self.description = description

    def defaults(self):
        return {
            name: parameter.default
            for name, parameter in self.parameters.items()
            if parameter.default is not None
        }

    def validate_parameters(self, parameters):
        """Validated copy of ``parameters``.

        An unknown key is an error rather than a passthrough: it is almost always a typo,
        and silently ignoring it means an admin who sets ``temprature`` watches nothing
        change and concludes the field does not work.
        """
        if not isinstance(parameters, dict):
            raise ValueError("Parameters must be a JSON object")
        cleaned = {}
        for name, value in parameters.items():
            parameter = self.parameters.get(name)
            if parameter is None:
                known = ", ".join(sorted(self.parameters)) or "none"
                raise ValueError(f"Unknown parameter {name!r}. This kind accepts: {known}")
            cleaned[name] = parameter.validate(value)
        return cleaned

    def __str__(self):  # pragma: no cover - admin/debug convenience
        return self.label


#: Speech-to-text over a WebSocket, streaming PCM up and transcripts down. An external
#: host and a service on an internal network are both this kind; they differ only in the
#: URL scheme and whether a CA is pinned.
STT_WEBSOCKET = ServiceKind(
    "stt_websocket",
    "Speech to text (WebSocket)",
    transport="websocket",
    allowed_schemes=("ws", "wss"),
    requires_api_key=True,
    description=(
        "Live dictation. Receives PCM16 mono 16 kHz frames and returns cumulative "
        "partial/final transcripts -- cumulative because the browser assigns the text it "
        "is sent rather than appending it."
    ),
    parameter_schema=(
        Parameter("beam_size", "Beam size", "int", default=1, minimum=1, maximum=10,
                  help="Greedy (1) is the right default for live dictation."),
        Parameter("vad_threshold", "Voice activity threshold", "float", default=0.5,
                  minimum=0.0, maximum=1.0,
                  help="How far above the measured noise floor counts as speech."),
        Parameter("min_segment_seconds", "Minimum segment (s)", "float", default=2.0,
                  minimum=0.2, maximum=30.0),
        Parameter("max_segment_seconds", "Maximum segment (s)", "float", default=12.0,
                  minimum=1.0, maximum=60.0,
                  help="A speaker who never pauses is committed at this ceiling."),
        Parameter("compute_type", "Compute type", "str", default="int8",
                  choices=("int8", "int8_float16", "float16", "float32"),
                  help="Read by the service at startup: changing it needs a restart."),
    ),
)

#: An OpenAI-compatible chat-completions endpoint. OpenRouter by default, but the same
#: row shape points at a self-hosted vLLM or Ollama without a code change -- which is the
#: escape hatch if sending clinical text to a third party is ever ruled out.
LLM_CHAT_OPENAI = ServiceKind(
    "llm_chat_openai",
    "LLM chat completions (OpenAI-compatible)",
    transport="http",
    allowed_schemes=("http", "https"),
    requires_api_key=True,
    requires_model=True,
    description=(
        "POSTs to {base_url}/chat/completions. Any OpenAI-compatible server: OpenRouter, "
        "vLLM, Ollama, LM Studio."
    ),
    parameter_schema=(
        Parameter("temperature", "Temperature", "float", default=0.2,
                  minimum=0.0, maximum=2.0,
                  help="Low on purpose: this reorganises dictated text, it does not write."),
        Parameter("top_p", "Top-p", "float", minimum=0.0, maximum=1.0),
        Parameter("max_tokens", "Maximum output tokens", "int", default=4000,
                  minimum=64, maximum=32000,
                  help="On a reasoning model this budget covers the thinking too, so it "
                       "must be well above the answer's length."),
        Parameter("reasoning_effort", "Reasoning effort", "str", default="low",
                  choices=("none", "minimal", "low", "medium", "high"),
                  help="Reasoning models spend this share of the token budget thinking "
                       "before they answer (low is about a fifth). Filing dictated text "
                       "under headings needs little of it, and a model that thinks past "
                       "the budget returns nothing at all."),
        Parameter("referer", "HTTP-Referer header", "str",
                  help="OpenRouter attributes usage to this. Optional."),
        Parameter("title", "X-Title header", "str",
                  help="Shown in the OpenRouter dashboard. Optional."),
    ),
)


SERVICE_KINDS = {kind.key: kind for kind in (STT_WEBSOCKET, LLM_CHAT_OPENAI)}


def kind_choices():
    """Django ``choices`` for the kind column.

    A callable, so adding a kind is not a migration -- Django 5 re-evaluates it rather
    than freezing the list into the field's deconstruction.
    """
    return [(kind.key, kind.label) for kind in SERVICE_KINDS.values()]


def kind_for(key):
    """The :class:`ServiceKind` for ``key``, or ``None`` for one that no longer exists.

    ``None`` rather than a raise: a row whose kind was removed in a later version must
    still be loadable in the admin so somebody can fix or delete it.
    """
    return SERVICE_KINDS.get(key)
