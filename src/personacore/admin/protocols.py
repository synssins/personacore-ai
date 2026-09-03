"""The seams the admin API talks through — spec sections 5.1, 5.5, 7, 9, 10.

Every collaborator arrives as a constructor argument to
:func:`personacore.admin.routes.create_admin_router`, and every one of them is
typed here as a ``Protocol`` rather than imported concretely. Three reasons,
all of them the same reason in different clothes:

* **The admin API is a contract (ADR-0007), and a contract that imports half
  the core is not one.** The real ``LLMClient``, ``EventBus``, ``AuditStore``
  and ``PersonaStore`` satisfy these structurally; nothing here reaches into
  them.
* **Tests must not need a broker, a database or an LLM host.** A protocol is
  the cheapest possible fake.
* **The plugin supervisor does not exist yet.** Spec section 5.1 requires a
  crashed plugin to be "surfaced in the admin UI", which needs runtime health
  the discovery scan cannot know. :class:`PluginHealthSource` is the shape that
  supervisor is expected to expose; until it exists the argument is optional
  and the API reports plugins as ``unknown`` rather than inventing a status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from personacore.api.keys import ApiKeyRecord, IssuedKey
from personacore.audit import AuditRecord, Owner, Surface, TranscriptRecord
from personacore.config.settings import CoreSettings
from personacore.contracts.policy import PolicyProfile
from personacore.conversations.addressing import FloorAnswer


class LLMHealthResult(Protocol):
    """The narrow view of ``personacore.llm.client.HealthStatus`` this API uses.

    Spec section 10 requires a health check on every dependency and a
    plain-English explanation when one is down; that is exactly these two
    fields, so nothing else is depended on.
    """

    healthy: bool
    detail: str | None


class LLMRoleView(Protocol):
    """One LLM role as the dashboard sees it — ADR-0011.

    "The LLM is up" stopped being a single fact when the core gained a set of
    named roles, so section 9's dashboard reports a row per role. This is the
    narrow shape a row needs: which role, whether it has an endpoint of its own
    or is falling back to ``interactive``, which endpoint it shares (so two
    roles pointed at one host are not probed twice), and the probe itself.
    """

    role: str
    """The role's name, e.g. ``interactive``."""

    falls_back_to: str | None
    """The role whose configuration this one is borrowing, or ``None`` when it
    is separately configured. Shown so the UI says "falling back" rather than
    implying five mandatory setups."""

    endpoint: str
    """Opaque, stable identity of the distinct endpoint behind this role.

    Roles sharing a value share a client and therefore a circuit breaker.
    Deliberately not the base URL: a hand-edited config can put credentials in
    one, and this is rendered into a page (spec section 7)."""

    facts: dict[str, object]
    """Free-form detail for the row — model in use, breaker state."""

    async def health_check(self) -> LLMHealthResult: ...


class LLMHealthSource(Protocol):
    """Anything that can answer "is the LLM host reachable?" — spec section 9's
    dashboard row. ``personacore.llm.client.LLMClient`` satisfies this.

    A source that knows about ADR-0011's roles also offers ``role_views()``;
    the dashboard reports a row per role when it is there and a single ``llm``
    row when it is not. It is not part of this Protocol because a plain client
    is a legitimate source — the CLI builds one — and requiring the roles
    method would make the narrow case describe itself as the broad one.
    """

    async def health_check(self) -> LLMHealthResult: ...


class BusHealthReport(Protocol):
    """``personacore.bus.client.BusHealth``'s public shape."""

    connected: bool
    last_error: str | None

    def as_dict(self) -> dict[str, object]: ...


class EventBusSource(Protocol):
    """``personacore.bus.client.EventBus`` seen through its health attribute
    only. The admin API never publishes, subscribes or reconnects."""

    health: BusHealthReport


class AuditGateway(Protocol):
    """The audit store as the admin API needs it — spec sections 7 and 9.

    Reads back the trace view, writes a record for every admin change (section
    7: "every admin change" is one of the four things the audit log must
    cover), and exposes ``schema_version`` so the health endpoint can prove the
    store is actually openable rather than merely present on disk.
    """

    @property
    def schema_version(self) -> int: ...

    async def record_audit(self, record: AuditRecord) -> AuditRecord: ...

    async def query_audit(
        self,
        *,
        owner: Owner | None = None,
        surface: Surface | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]: ...

    async def query_transcript(
        self,
        *,
        owner: Owner | None = None,
        surface: Surface | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[TranscriptRecord]: ...


class ApiKeyGateway(Protocol):
    """The API-key store as the admin API needs it — spec sections 5.4 and 9.

    Section 5.4 says keys are "issued and revoked in the admin UI"; section 9
    lists "user/profile management and API-key issuance" as an admin screen.
    This is the seam between the two halves of that sentence:
    ``personacore.api.keys.ApiKeyStore`` owns the credential — minting it,
    hashing it, persisting it, comparing it in constant time — and the admin
    API owns only the screen it is issued from. **Nothing in this package
    hashes, compares or stores key material, and nothing here should ever
    start.**

    ``verify`` **used to be deliberately absent**, on the reasoning that the
    admin API authenticates operators through the reverse proxy and that
    checking a client key here would be "an invitation to grow a second front
    door". That turned out to be the wrong half of the sentence to worry about:
    the admin JSON API's only credential was a sign-in, so any household member
    with an account could reach it — and issue themselves keys to ``/v1`` and
    read everybody's conversations. The second front door was already there. It
    is now the *only* door on ``/admin/api`` (see
    :class:`personacore.admin.authn.AdminApiKeyDoor`), which is what a key check
    here is for. Nothing in this package hashes or compares key material still:
    ``verify`` is asked, and the store answers.

    The concrete ``ApiKeyStore`` satisfies this structurally, as with every
    other collaborator here — but this one is typed against its models
    (:class:`~personacore.api.keys.ApiKeyRecord`,
    :class:`~personacore.api.keys.IssuedKey`) rather than restating them,
    because a parallel definition of "what an issued key looks like" is exactly
    the kind of drift that ends with two ideas of what a key is.
    """

    def verify(self, presented: str | None) -> ApiKeyRecord | None: ...

    def records(self) -> list[ApiKeyRecord]: ...

    def get(self, key_id: str) -> ApiKeyRecord | None: ...

    def issue(self, *, profile: PolicyProfile, note: str = "") -> IssuedKey: ...

    def revoke(self, key_id: str) -> bool: ...


class PluginRuntimeStatus(Protocol):
    """What a plugin supervisor knows that a directory scan cannot: whether the
    process is up, why it died, and how often it has been restarted (spec
    section 5.1)."""

    state: str
    """``running`` / ``stopped`` / ``crashed`` / ``starting`` — free text so a
    supervisor that has not been written yet is not boxed in by this file."""

    detail: str | None
    """Plain-English reason when the state is not ``running`` (spec 9)."""

    restarts: int

    # Optional, checked with getattr like `reload` below. A supervisor that
    # holds a plugin back for a credential it was never given should also expose
    # `waiting_for_secrets: tuple[str, ...]` — the NAMES, never a value
    # (ADR-0025 section 5). Not declared required, because a supervisor that
    # only reports up/down is still a valid source; but a source that drops it
    # leaves the listing reporting a plugin waiting for an API key as
    # `failing`, which is a fault it does not have and a word the page beside it
    # does not use.


class PluginOutputView(Protocol):
    """What one plugin printed to its own stderr — spec section 9 (PC-279).

    ``personacore.plugins.health.PluginOutput`` satisfies this structurally.

    **The text is untrusted.** It is output from third-party code, so it is
    escaped and rendered as text wherever it lands, never as markup (spec
    section 7). The two flags are separate because they answer different
    questions: ``clipped`` is "you are looking at the end of a longer capture",
    ``dropped`` is "the earlier part no longer exists". A page that showed a
    partial tail as though it were the whole thing would be actively
    misleading somebody who is debugging.
    """

    text: str
    dropped: bool
    clipped: bool


class PluginHealthSource(Protocol):
    """The plugin supervisor's health view, keyed by plugin name.

    Optional everywhere it is used: P0's discovery layer can report *load*
    failures without it, and reporting "unknown" is honest where inventing
    "healthy" would not be.
    """

    def status_for(self, plugin_name: str) -> PluginRuntimeStatus | None: ...

    # Optional. A supervisor that captures a plugin's stderr should also expose
    # `def output_for(self, plugin_name: str) -> PluginOutputView | None`, which
    # the plugin output page reads (PC-279). `None` from it means "nothing is
    # known about that plugin's output" — an HTTP-transport plugin runs in its
    # own container and its stderr is that container's — which the page says in
    # those words rather than showing an empty box. Checked with getattr rather
    # than declared required, for the same reason `reload` below is: a source
    # that only reports status is still a valid source.

    # Optional. A supervisor that can start and stop plugins should also expose
    # `async def reload(self) -> object`, which the reload endpoint calls before
    # rescanning. Rescanning alone only updates the listing; spec section 5.1
    # promises the plugin actually works after reload, which means the
    # supervisor has to start it. Checked with getattr rather than declared
    # required, so a source that only reports status is still valid.


class PluginToggle(Protocol):
    """Switching one plugin on or off in the *running* core — ADR-0013.

    Deliberately separate from :class:`PluginHealthSource`. That protocol
    describes something that can *report*; this one describes something that
    can *act*, and the two are satisfied by different objects in some
    assemblies (a read-only health adapter over a host that can start and stop
    processes). Keeping them apart means a core wired with only the reporting
    half still builds, and the toggle endpoints say so rather than pretending
    to have worked.

    ``personacore.plugins.host.PluginHost.set_enabled`` satisfies this
    structurally, as everything else here does.

    Persisting the choice is **not** this seam's job: the admin API writes it to
    appdata itself (``personacore.plugins.packages.set_plugin_enabled``) so the
    state survives a restart whether or not a toggle was supplied.
    """

    async def set_enabled(self, name: str, enabled: bool) -> object: ...

    # Optional, and checked with getattr for the same reason `reload` above is:
    # a toggle that can only switch a plugin on and off is still a valid
    # toggle, and an assembly wired with one still uninstalls.
    #
    # A host that keeps state ABOUT a plugin — a switched-off set, a
    # load-failure row, a captured stderr — should also expose `async def
    # forget(self, name: str) -> None`, which the uninstall endpoint calls once
    # the folder is gone. Uninstall deletes what is on disk; this is the half
    # that lives in memory, and its absence was a real defect rather than a
    # tidiness point: uninstall stops the plugin first (`set_enabled(name,
    # False)`, so a live subprocess is not holding its own files open while
    # they are deleted), which put the name in the running switched-off set,
    # and nothing ever took it out. **Installing the same plugin again
    # installed a plugin that never started**, with nothing on screen saying
    # why. `personacore.plugins.host.PluginHost.forget` satisfies it, and is
    # idempotent and safe for a name that was never installed.


class ToolCallOutcome(Protocol):
    """One finished plugin tool call, seen through the three things a settings
    page needs. ``personacore.agent.protocols.ToolResult`` satisfies it.

    ``content`` is **untrusted**: it came from a plugin, which got it from
    wherever it looks things up. ADR-0016 is explicit that lookup results "are
    rendered as data in a list, never interpreted, and never used to build a
    path or a request", and everything downstream of this protocol treats it
    that way.
    """

    ok: bool
    content: str
    error: str | None


class PluginToolCaller(Protocol):
    """Calls one plugin tool by name — ADR-0016's search-and-fill.

    ``personacore.plugins.host.PluginHost.call_tool`` satisfies this
    structurally, and passing that bound method is the intended assembly: it
    already enforces the manifest's declared risk, applies the plugin's declared
    permissions, records the call in the audit log and puts it in the trace,
    which is exactly the list of properties ADR-0016 requires of a lookup ("It
    is a tool call like any other").

    Optional everywhere it is used. A core assembled without a plugin host still
    builds and its settings pages still work; a field that declared a lookup
    simply says the search is unavailable, rather than the page pretending the
    feature does not exist (the same rule as the API-key notice).

    ``name`` is ``"<plugin>.<tool>"``. The admin package never lets a *request*
    choose it: the plugin comes from the path and the tool comes from the
    plugin's own schema, checked against its manifest first.

    ``owner`` and ``surface`` are the operator who pressed search and the
    surface they pressed it on. The callee writes this call's audit record, and
    without them it files it against the household on ``system`` — hidden from
    the trace view's user and surface filters, and aged out on the wrong
    per-surface retention window (ADR-0004). They are typed ``Any`` for the
    same reason ``risk_ceiling`` is: this protocol does not import the audit
    models.
    """

    async def __call__(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        risk_ceiling: Any = None,
        owner: Any = None,
        surface: Any = None,
    ) -> ToolCallOutcome: ...


class SecretNameSource(Protocol):
    """Something that can list the *names* of the secrets this core holds.

    ADR-0015: a plugin setting may be marked as holding the name of a secret,
    "and the UI offers the names in the store. Values never leave it." This
    protocol is the whole of that seam, and it is deliberately name-only —
    there is no method here that returns a value, so the admin package cannot
    render one even by mistake (spec section 7).

    ``personacore.config.secrets.SecretStore.available`` satisfies it
    structurally, as everything else here does. It lists the CORE's namespace:
    since ADR-0025 a secret belongs to an owner, and the names an operator may
    choose between on a core settings field are the core's own.
    """

    def available(self) -> list[str]: ...

    # Optional, checked with getattr like `reload` on PluginHealthSource. A
    # store that can delete should also expose `def delete_namespace(self,
    # plugin: str) -> int`, returning how many secrets went; the uninstall
    # endpoint calls it once the plugin's folder is gone. Without it a
    # reinstalled plugin of the same name inherits the previous install's
    # credentials — a leak, and a surprise, in the one direction ADR-0025's
    # namespacing exists to close.
    #
    # It is the ONLY method here that changes anything, and it still returns no
    # value: a count of what was removed is not a secret. `SecretStore`'s own
    # containment check refuses anything that is not a real directory inside
    # the plugins namespace, so this module passes the plugin's name and writes
    # no path logic of its own (spec section 7).


class ChatTurnResult(Protocol):
    """One finished agent turn, seen through the things a page must show.

    Spec section 9's dashboard is about *"is it working"*, and the honest
    answer to that needs a reply, the persona that produced it, and — when the
    turn failed — a sentence saying why in plain English rather than a
    traceback. Structural, like every other type here, so the agent loop can
    return whatever object it already has.

    ``tools_offered`` and ``tools_called`` were added because the page could not
    answer the first question anyone asks of an assistant that says it has no
    tools: *were any sent?* A healthy installed plugin whose schema went out and
    was ignored, and a plugin whose tools never reached the model, produce the
    same sentence from the model and have completely different fixes — and
    without this, telling them apart meant capturing the outgoing HTTP request.
    Spec section 9 ("if you can't see what it did, you can't trust it or debug
    it") is the same argument the trace view is built on.

    **Both are read defensively.** The chat screen uses ``getattr`` with a default
    of ``None``, so a runner that predates these fields still renders — and
    ``None`` reads on the page as "not reported", never as "none offered". A
    turn that genuinely offered nothing reports an empty sequence, and those are
    different facts.
    """

    ok: bool
    """False when the turn did not produce an answer. Not inferable from an
    empty ``reply``: a model that legitimately says nothing and a model that
    was never reached are different states, and only one is a fault."""

    reply: str
    persona: str
    """Which persona answered (spec section 5.5). Shown so a persona swap can
    be *seen* to have taken effect rather than assumed."""

    error: str | None
    """Plain English, shown verbatim. None when ``ok``."""

    tools_offered: Sequence[str]
    """Names of every tool whose schema was sent to the model for this turn.

    The count the page shows is ``len()`` of this: one field rather than a
    number *and* a list, because two fields that must agree eventually will
    not. Empty means the model was offered nothing — which is a real answer and
    usually the bug."""

    tools_called: Sequence[str]
    """Names the model actually invoked, in the order it invoked them.

    Empty when it called none. Offered-but-not-called is the ordinary case for
    a model that decided a tool was not needed, and saying so out loud is the
    whole point: it is not a fault, and it looks exactly like one."""

    #: **Not a member of this protocol, and read the same defensive way**
    #: ``tools_offered`` is: a runner may also carry ``speech``, which is how
    #: the reply can be heard (PC-256). It is a
    #: :class:`personacore.voice.reply.ReplySpeech` — ``can_speak``,
    #: ``audio_url`` (set exactly when ``can_speak``), ``reason`` (the PC-336
    #: sentence when a chosen voice cannot be heard, ``None`` when there is
    #: nothing to say) and ``voice_label``. A screen renders a player from the
    #: URL and the sentence from ``reason``; ``getattr(turn, "speech", None)``
    #: covers a runner assembled without voice, and a reply with no audio is an
    #: ordinary reply rather than a failure.
    #:
    #: It is documented here rather than declared above deliberately: making it
    #: a required member would make every runner that predates it stop
    #: satisfying this protocol, and speech is an addition to a reply, never a
    #: condition of one.


class ChatHistoryTurn(Protocol):
    """One earlier message of the conversation a turn continues.

    Structural, and deliberately only the two fields the agent loop's own
    ``ConversationMessage`` accepts: the admin package must not import the
    agent loop (see this file's header), and a history entry that could carry a
    ``system`` role would be a way past the persona and the safety block
    (ADR-0005) opened from a web form.

    ``role`` is ``user`` or ``assistant``. Nothing else is a conversation turn
    — a tool result belongs to the round that produced it, and the transcript
    store's ``tool`` rows are filtered out before they reach here.
    """

    role: str
    content: str


class ChatRunner(Protocol):
    """Runs one non-streaming turn through the agent loop — ADR-0007.

    The narrowest seam that lets the admin chat screen prove the persona and
    the LLM connection work *together*, which no health check can: a reachable
    host with a wrong model name passes every check and still cannot answer.

    Deliberately a callable rather than the agent loop itself. The admin
    package must not import the agent loop — the reasons at the top of this
    file all apply, and this one most of all: a presentation layer that could
    reach into the loop's internals would stop being replaceable, which is what
    ADR-0007's API-first split bought.

    ``user`` is the operator the reverse proxy identified, passed on so the
    turn is attributed to a real person in the transcript (spec section 7)
    instead of appearing to come from the core itself.

    ``history`` is the conversation so far, oldest first, and defaults to
    empty — a runner is never obliged to be given one, and a caller that wants
    a single stateless turn simply omits it. It is
    a *parameter* rather than state inside the runner on purpose: session state
    that lives in the process is state that disagrees with the transcript after
    a restart, and the transcript is the record spec section 9 says has to be
    the truth.

    ``persona`` is who should answer *this turn*, and it is a parameter for the
    same reason ``history`` is. Before it existed, the only way to change who
    answered on the chat screen was to move ``[core] default_persona`` — which
    changed the character of the OpenAI-compatible API, of an event waking the
    agent, and of everything else in the house at the same time. A turn that
    carries its own persona lets the default stay mild for the doorbell while
    somebody talks to the snarky one, and choosing here writes no config at
    all.

    ``None`` — and omitting it entirely — means the configured default, which
    is what every caller that predates this got and still gets. It is
    deliberately a *name*, resolved by the runner through the same persona
    store the default is resolved through, so the safety block and the
    persona's own guardrails (ADR-0005) sit in front of a named persona exactly
    as they sit in front of the default one. Naming a persona chooses a
    character; it is never a route past anything.

    A name that does not exist, or that this caller may not use, is
    **refused** — an unsuccessful :class:`ChatTurnResult` carrying a plain
    sentence. It is never quietly answered by the default: an operator who
    asked for GLaDOS and got a polite stranger has been lied to about who they
    are talking to.

    ``record_user_message`` is ``True`` for every turn a person started and
    ``False`` for exactly one case: a persona answering another persona in the
    same room (the many-voices contract, §3.3). The words prompting that turn
    are the previous character's reply, which is already in the transcript
    attributed to the character that said it; writing it again as a ``user``
    row would draw another persona's words on the screen as a message somebody
    typed.

    ``also_present`` is the **display names** of the other personas in the room
    for this turn (§2), and it is what tells a persona anybody else is there at
    all. Without it a character has no way to know it is not alone, so §3.3 —
    a persona addressing another by name — can never start; the roster was data
    the prompt never saw. Display names because they are what the persona has
    to type to reach somebody (§3.1 matches the name on the screen).

    **Empty is a room of one**, and it is the default, so a caller that has
    never heard of rooms composes the prompt it always did. It is a parameter
    for the same reason ``history`` and ``persona`` are: a room is a property
    of the conversation the screen is drawing, and it is composed fresh every
    turn, which is what lets a persona who left be gone from the next one.

    ``image_data_urls`` is an attached image, as one or more ``data:`` URIs —
    attachments contract §4.2. **Empty is the default and composes the prompt
    exactly as it always has** — the same rule ``also_present`` follows, for
    the same reason: a runner that predates this keyword simply never sees
    it, discovered with ``inspect.signature`` before it is offered
    (``chat_exchange._takes``), so an older runner is asked for nothing it
    cannot do rather than raising ``TypeError`` mid-turn. There is no gate
    here either: contract §4.3 says the vision probe never blocks the send
    path, and a runner is not the place to start.
    """

    async def __call__(
        self,
        message: str,
        *,
        user: str,
        history: Sequence[ChatHistoryTurn] = (),
        persona: str | None = None,
        record_user_message: bool = True,
        also_present: Sequence[str] = (),
        image_data_urls: Sequence[str] = (),
    ) -> ChatTurnResult: ...


class ChatStreamEvent(Protocol):
    """One thing that happened part-way through a turn.

    The agent loop has emitted these all along (its ``AgentEvent``); what did
    not exist was a way for the admin surface to *see* them. Chat collected the
    whole turn and rendered it at the end, so a forty-sentence reply appeared
    as one wall after twenty-five seconds of nothing and the audio could not
    start until it did.

    Structural and deliberately narrow — the admin package must not import the
    agent loop (see this file's header), and the loop's event carries several
    things a screen has no business acting on. ``kind`` is the whole of the
    vocabulary:

    ``text``
        ``text`` is the next fragment of the reply. Print it and speak it.
    ``reasoning``
        ``text`` is the next fragment of the model's own thinking
        (``reasoning_content`` on the wire), never part of the reply and never
        spoken. Shown live and only live — nothing here is meant to survive a
        reload; a screen that does not know this ``kind`` simply never sees it
        rendered, same as any other unknown kind.
    ``tool_call``
        ``tool_name`` is about to run *inside this container*.
    ``tool_result``
        that tool finished; ``duration_ms`` is how long it took.
    ``notice``
        ``text`` is a plain-English statement about the assistant's own
        condition — the model is unreachable, the persona is missing.
        Speakable verbatim.
    ``done``
        the end. ``result`` is the finished :class:`ChatTurnResult`, the same
        object the non-streaming runner returns, so one turn renders through
        one function whichever way it was run.

    An unknown ``kind`` is ignored rather than refused: a loop that grows a
    sixth event must not break a screen that only knows five.
    """

    kind: str
    text: str
    tool_name: str | None
    duration_ms: float | None
    result: ChatTurnResult | None


class ChatStreamRunner(Protocol):
    """Runs one turn and reports it as it happens.

    The same turn :class:`ChatRunner` runs — same persona, same policy, same
    audit trail, same transcript — differing only in when the caller hears
    about it. Every argument means exactly what it means there, including
    ``image_data_urls`` (attachments contract §4.2).

    Discovered rather than required: it is an optional ``stream`` attribute on
    the runner the admin surface is already given, found with ``getattr``, and
    a core assembled without one falls back to the non-streaming path. The
    reason is the one every optional seam here has — a runner that predates
    this must keep working — and the consequence is worth stating: Chat is a
    conversation with or without it, and only the *feeling* of it changes.

    **The stream is closed by whoever iterates it.** A browser that goes away
    mid-reply is the ordinary case rather than the exception, and an
    abandoned async generator holds the turn, the model connection and
    everything downstream of it open. The caller closes; the runner cleans up
    what it opened.
    """

    def __call__(
        self,
        message: str,
        *,
        user: str,
        history: Sequence[ChatHistoryTurn] = (),
        persona: str | None = None,
        record_user_message: bool = True,
        also_present: Sequence[str] = (),
        image_data_urls: Sequence[str] = (),
    ) -> AsyncIterator[ChatStreamEvent]: ...


class ChatFloorAsk(Protocol):
    """Puts one short question to one persona — the many-voices contract §3.2.

    A message in a room that names nobody opens the floor, and every persona
    present is asked whether it wants to answer. That is one model call per
    persona per turn, which is why §3.1's name matching exists and why this is
    reached for only when that found nothing.

    Discovered rather than required, exactly as :class:`ChatStreamRunner` is:
    an optional ``ask`` attribute on the runner the admin surface already
    holds, found with ``getattr``. A core assembled without one cannot open the
    floor, so a room with two personas in it answers only when somebody is
    named — a smaller feature, never a broken screen.

    ``persona`` is required and is never ``None``: the question is *"is this
    for you"*, which the configured default cannot be asked on somebody else's
    behalf.

    ``extra_body`` is merged into the model request. It carries
    :data:`~personacore.conversations.addressing.FLOOR_NO_THINKING` — the hint
    that stops a reasoning model spending the whole token ceiling on thinking
    and returning nothing, which is what it did on every floor question during
    testing. An implementation that sends it must cope with a host that
    refuses it.

    **The answer is the persona's own words, unread.** Whether they mean yes is
    :func:`personacore.conversations.addressing.claims_floor`'s decision and
    lives with the question that was asked. An empty
    :class:`~personacore.conversations.addressing.FloorAnswer` is every kind of
    failure — an unreachable model, a persona that stopped being installed —
    and it reads as no, which is the direction §3.2 says to fail in: a persona
    that could not say it wanted the floor does not get it.

    It is a value rather than a string because the words alone could not tell a
    considered no from a call that ran out of budget before the persona reached
    the question, and that is precisely how the reasoning-model defect stayed
    invisible.
    """

    async def __call__(
        self,
        question: str,
        *,
        user: str,
        persona: str,
        history: Sequence[ChatHistoryTurn] = (),
        max_tokens: int = 8,
        extra_body: Mapping[str, Any] | None = None,
    ) -> FloorAnswer: ...


class SettingsApplier(Protocol):
    """Called after core settings are successfully written to disk.

    The admin API owns persisting ``core.toml``; it deliberately does **not**
    own the live objects built from it (the ``PersonaStore``'s default persona,
    the LLM client's base URL, the bus connection). Whoever assembled those
    passes a callback here so a settings change reaches them without this
    module reaching into anyone's private state.
    """

    def __call__(self, settings: CoreSettings) -> None: ...


__all__ = [
    "ApiKeyGateway",
    "AuditGateway",
    "BusHealthReport",
    "ChatFloorAsk",
    "ChatHistoryTurn",
    "ChatRunner",
    "ChatStreamEvent",
    "ChatStreamRunner",
    "ChatTurnResult",
    "EventBusSource",
    "FloorAnswer",
    "LLMHealthResult",
    "LLMHealthSource",
    "LLMRoleView",
    "PluginHealthSource",
    "PluginOutputView",
    "PluginRuntimeStatus",
    "PluginToggle",
    "PluginToolCaller",
    "SecretNameSource",
    "SettingsApplier",
    "ToolCallOutcome",
]
