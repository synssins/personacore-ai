/* chat.js — the six things the chat screen needs a script for.

   Nothing here is required for the conversation to work. Typing a message and
   getting a reply is a form post the server answers with a redirect, and htmx
   turns the same form into an append. This file makes it feel immediate, and
   adds dictation, which genuinely cannot be done without a script.

   1. IMMEDIATE ACKNOWLEDGEMENT. The question is drawn the instant Send is
      pressed, marked pending, and removed when the server's rendering of the
      same exchange arrives. Nothing waits on a round trip to admit that a key
      was pressed.
   2. SCROLL DISCIPLINE. The message list follows the newest message only when
      the reader was already at the bottom of it. Somebody scrolled up reading
      yesterday is left exactly where they are — a reply must not yank the page
      out from under the thing being read.
   3. THE REPLY AS IT IS WRITTEN, and the audio as it is spoken. The turn is
      posted to /admin/chat/stream, which answers in server-sent events: the
      words as the model produces them, and the address of the audio as soon as
      the first sentence has certainly ended. The finished exchange arrives as
      the last frame, rendered by the server, and replaces the growing text —
      so the markdown, the player and the footer are the same markup the
      non-streaming path produces.
      THE TURN IS NOT THIS FETCH. It runs on the server whether or not anybody
      is watching it, so this file re-attaches to one it lost sight of and
      attaches on load to one already running — and, because closing the tab no
      longer ends anything, the composer carries the button that does.
   4. MANY VOICES IN ONE ROOM. A conversation can hold more than one persona,
      and the stream says which one is speaking: each character gets a bubble
      under its own name, its finished reply lands as the server renders it,
      and the stop button appears for as long as the room is talking. THE AUDIO
      QUEUES — one voice at a time, never two at once — while the text is not
      held back for a moment.
   5. THE MICROPHONE, and the truth about it. The browser's own recogniser,
      which sends the audio to Google. The control is hidden until a recogniser
      is found, the disclosure is revealed with it, and a live badge says where
      the audio is going for as long as it is going there.
   6. THE TOOLBAR UNDER A REPLY. Copy puts the reply's plain text — never the
      rendered markup — on the clipboard and says so in the same words a saved
      form uses. The whole row is hidden until this file has found somewhere to
      write, because a clipboard cannot be reached without a script and a
      button that cannot do anything is worse than no button at all.
   7. SELECTING SEVERAL CONVERSATIONS AT ONCE (docs/contracts/conversation-
      list-bulk-actions.md). The checkbox, the select-all checkbox and the
      Delete button are ordinary form controls that already work with no
      script at all — see chat.html and fragments/chat_threads.html, and
      test_chat_bulk_delete.py, which proves the whole mechanism with this
      file never loaded. What this section ADDS is layered on top of that,
      never a second way of selecting something: Delete hides itself until a
      row is checked, the select-all checkbox visibly checks every row rather
      than only being read at submit time, and a long press on a row — the
      gesture a touch screen has instead of a pointer hovering a checkbox —
      toggles that row the same way clicking its checkbox would. The desktop
      form is still the truth; long-press only ever changes the same
      checkboxes it draws (contract §3).
   8. A MESSAGE SENT WHILE A REPLY IS BEING WRITTEN WAITS ITS TURN
      (docs/contracts/composer.md §3). It used to cancel the reply in
      progress; only stop stops now. One message waits, it is visible while it
      waits, and it can be taken back before it goes.
   9. RESEND (§7). The second control on the bar under a message asks it
      again, as a new turn rather than an edit. It is a plain form and needs
      none of this file to work; what this adds is the streaming and §3.

   NOTHING IN 8 OR 9 IS THE ONLY WAY TO DO EITHER. A browser with no script
   posts the composer and gets the answer back in one piece, and the resend
   form is an ordinary post to the same route the composer uses.

   NO EXTENSION AND NO LIBRARY. Streaming uses fetch + ReadableStream, which
   every browser this admin UI runs in has, and EventSource is not used because
   it can only issue a GET — the turn is a POST carrying the message. A browser
   without those three objects never takes this path: htmx makes the ordinary
   request and the reply arrives in one piece, which is what it did before.

   Loaded once. hx-boost replaces the body, which re-runs this file, so
   everything below is idempotent and every listener is on `document`. */
(function () {
  'use strict';
  if (window.__personacoreChat) return;
  window.__personacoreChat = true;

  var NEAR_BOTTOM = 80; // px from the end that still counts as "at the end"

  function messages() { return document.getElementById('chat-messages'); }
  function atBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM;
  }
  function toBottom(el) { if (el) el.scrollTop = el.scrollHeight; }

  // Open the screen at the newest message rather than the oldest.
  //
  // Deliberately still only the scroll. Attaching to a turn already running
  // belongs at the BOTTOM of this file, not here: `settle()` runs the moment
  // this script is parsed, and every `var` declared below is hoisted but not
  // yet assigned at that point — so `streamable` would be `undefined` and the
  // attach would silently decline every time. See the end of the file.
  function settle() { toBottom(messages()); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', settle);
  } else {
    settle();
  }
  // ...including when the screen arrived through a boosted navigation rather
  // than a page load. hx-boost replaces the body's contents, so this file does
  // not run again — every listener here is on `document`, which does not go
  // away, and this is what stands in for the load that did not happen.
  document.addEventListener('htmx:afterSettle', function (e) {
    var target = e.target;
    if (!target || !target.querySelector || !target.querySelector('#chat-messages')) return;
    // A whole-page swap mid-turn — chat_room.py always re-renders the entire
    // page, and renaming, the persona picker, the group change and the menu
    // fold all go through it — detaches whatever `send()` was writing into.
    // `reply`/`replyText`/`replyAuthor` are held at module scope precisely so
    // they outlive that: put the words back in a bubble that belongs to the
    // page that just landed, before deciding where the list scrolls.
    var box = messages();
    if (box && inFlight && reply) {
      reply = bubble(box, replyAuthor);
      reply.textContent = replyText;
    }
    // `thinking`, the same way, and inserted with `insertBefore(el, reply)`
    // inside `thinkBubble` — so it lands above the reply just recreated
    // above regardless of which of the two this file rebuilds first.
    if (box && inFlight && thinkingText) {
      thinking = thinkBubble(box, reply);
      thinking.textContent = thinkingText;
    }
    // §5's own indicators are looked up fresh every time they are set, so the
    // new page's copies default to off. Put back whatever the stream's state
    // actually is, the same as the reply itself — a swap mid-turn must not
    // read as "stopped responding" when it did not.
    if (inFlight) {
      waiting(streamWaiting);
      stopButton(!!stopToken);
    }
    // The page that just landed brought a fresh send/stop button in its
    // resting state and a fresh `data-turn-running`. Put the button back into
    // the state this turn is actually in, and attach if that page says a reply
    // is still being written in this thread.
    composerButton();
    // The page that landed brought an empty queued row (the server never
    // renders one — see chat.html). A message still waiting behind the running
    // turn is held at module scope like the reply is, and is put back on the
    // screen here for the same reason: a whole-page swap mid-turn must not
    // read as "it forgot what I typed".
    renderQueued();
    attachIfTurnRunning();
    settle();
  });

  // ── 1. the question, drawn immediately ──
  //
  // A placeholder this file owns and this file removes. The server still
  // renders the real question with the reply, so a browser that never ran this
  // shows the same conversation — the placeholder is an optimism, not a state.
  var pendingParams = null;

  // ── whether the conversation is allowed to move under the reader ──
  //
  // On a host where a reply takes a minute or two, scrolling up to read
  // something earlier while one arrives dragged the reader straight back down
  // again. Two separate things did that, and both are answered here.
  //
  // `following` is the answer to "is the reader at the end of the conversation
  // *right now*". It used to be read once, at send time, and then consulted on
  // every frame for the next two minutes — by which point it was answering a
  // question about a page the reader had since scrolled. It is now written immediately
  // before whatever is about to make the list longer, and never earlier.
  //
  // THE ORDER IS THE WHOLE OF IT: read it *before* the list grows. Afterwards
  // the reader is no longer near the bottom of it and the answer is always no,
  // which is the same bug facing the other way — a reply that never follows
  // anyone.
  var following = true;

  // Set only for the swap that opens a different conversation, and cleared the
  // moment it is used. That is a fresh view rather than a position somebody
  // chose, so it still lands on the newest message.
  var openingAnother = false;

  // Do the thing that makes the conversation longer, then follow it only if
  // the reader was following before it grew.
  // NOT `grow`. That name already belongs to the composer's textarea auto-sizer
  // further down this file, and a second `function grow` in the same scope
  // silently replaces the first — so every streamed append called the textarea
  // sizer, which ignores the callback and never ran it. A persona was asked for a
  // story, the whole thing was read aloud, and no text appeared at all: the audio
  // is a separate fetch and did not care. `node --check` passes on it, because
  // two functions with one name is valid JavaScript.
  function appendKeepingPlace(box, change) {
    following = !box || atBottom(box);
    change();
    if (following) toBottom(box);
  }

  // What actually asked for a swap.
  //
  // NOT `e.detail.elt`, which is what this file used to read. htmx sets
  // `detail.elt` to the element the event is dispatched on, and every swap on
  // this screen is dispatched on `#chat-messages` — so `detail.elt.id` is
  // "chat-messages" for the composer, for a thread in the rail and for a
  // streamed exchange alike, and the two `'chat-form'` tests that used to be
  // written against it could never be true. One of them was meant to stop the
  // screen jumping and instead let it jump every time; the other was meant to
  // take down the pending question and never ran at all.
  //
  // `requestConfig.elt` is the control that made the request. It is present on
  // both `htmx:beforeSwap` and `htmx:afterSwap`.
  function askedBy(detail) {
    return (detail && detail.requestConfig && detail.requestConfig.elt) || null;
  }

  // The question, drawn where the reply will be, marked pending.
  //
  // ITS OWN FUNCTION because there are two moments a message is first seen:
  // the instant Send is pressed, and — since contract §3 — the moment a
  // message that had to wait for a running turn finally goes. The second is
  // minutes after the first, and a message that appeared, went back into a
  // chip above the box and then never came back would read as lost. One
  // drawing, so the two cannot drift.
  function drawQuestion(box, text, files, who) {
    if (!box || !text) return;
    var empty = document.getElementById('chat-empty');
    if (empty) empty.remove();

    var pending = document.createElement('div');
    pending.className = 'stack';
    pending.setAttribute('data-pending', '');
    pending.style.gap = 'var(--space-3)';
    // The name over the question, so the message does not gain a header when
    // the server's rendering of it replaces this. The server put it on the
    // form; this file never composes a name of its own, because the
    // parentheses rule that tells a person from a persona is decided in one
    // place and that place is chat.py.
    if (who) {
      var name = document.createElement('p');
      name.className = 'chat-said';
      name.style.alignSelf = 'flex-end';
      name.textContent = who;
      pending.appendChild(name);
    }
    // The tiles for whatever is attached, above the words and aligned with
    // them -- contract §6a puts a sent message's attachments above its text.
    //
    // Drawn here because the composer's own row is emptied the moment this
    // posts, and the server's rendering of the same message does not land
    // until the reply does. Mid-turn, the attachment tile appeared in the chat
    // window and then, on send, did not show up in the chat itself.
    // Nothing was lost -- the chips came back at the end -- but a
    // thing you attached vanishing for the length of a long reply reads as
    // having failed to attach, and on a long reply that is minutes of doubt.
    //
    // `attachSummaryTile`, not `attachTile`: the composer's tile carries a
    // remove `X`, and this message has already been sent. Offering to cancel
    // something that has gone is the same class of lie as a control that
    // looks live and is not.
    if (files && files.length) {
      var carried = document.createElement('div');
      carried.className = 'attach-row';
      carried.style.alignSelf = 'flex-end';
      for (var t = 0; t < files.length; t++) {
        carried.appendChild(attachSummaryTile(files[t]));
      }
      pending.appendChild(carried);
    }

    var said = document.createElement('p');
    said.style.cssText =
      'align-self:flex-end; max-width:80%; margin:0; padding:var(--space-3) var(--space-4);' +
      'border-radius:var(--radius-md); background:var(--color-accent-900); font-size:14px;' +
      'white-space:pre-wrap';
    // textContent, never innerHTML: this is what the operator just typed.
    said.textContent = text;
    pending.appendChild(said);
    box.appendChild(pending);
    toBottom(box);
  }

  document.addEventListener('htmx:configRequest', function (e) {
    var form = e.detail.elt;
    if (!form || form.id !== 'chat-form') return;
    var box = messages();
    var text = (e.detail.parameters.message || '').toString().trim();
    // Kept, because this handler is about to empty the textarea and
    // `beforeRequest` fires after it. Reading the form there found a box that
    // had already been cleared, so every message posted blank and the screen
    // answered "There was nothing to send" to a question it had just drawn on
    // itself. htmx has already gathered the fields here; these are them.
    pendingParams = e.detail.parameters;
    if (!box || !text) return;

    drawQuestion(box, text, pendingFiles, form.getAttribute('data-author'));

    var input = document.getElementById('chat-input');
    if (input) { input.value = ''; grow(input); input.focus(); }
  });

  function clearPending() {
    var stale = document.querySelectorAll('[data-pending]');
    for (var i = 0; i < stale.length; i++) stale[i].remove();
  }
  // The last moment the page still looks the way the reader left it, so it is
  // where both decisions about the swap are made.
  document.addEventListener('htmx:beforeSwap', function (e) {
    var box = messages();
    var detail = e.detail || {};
    var target = detail.target;
    var into = !!box && !!target && (target === box || box.contains(target));
    if (!into) {
      // A boosted navigation replaces the whole body and is not this screen
      // moving under anybody; `htmx:afterSettle` opens the new screen at its
      // newest message. Clear the flag so it cannot be spent on the next swap.
      openingAnother = false;
      return;
    }
    var from = askedBy(detail);
    // Named positively — a thread in the rail — rather than as "anything that
    // is not the composer". Only two controls swap into this list, and the one
    // that replaces it is the one that opens a different conversation; a third
    // that appended to it would otherwise inherit the jump by default.
    openingAnother = !!from && !!from.closest && !!from.closest('.chat-thread');
    following = atBottom(box);
    if (from && from.id === 'chat-form') clearPending();
  });
  // A request that never produced a swap — the network went, the server said
  // no — must not leave a question sitting there looking sent.
  document.addEventListener('htmx:afterRequest', function (e) {
    if (e.detail.elt && e.detail.elt.id === 'chat-form' && !e.detail.successful) clearPending();
  });

  // ── 2. follow the conversation, but only if the reader was following it ──
  document.addEventListener('htmx:afterSwap', function (e) {
    var box = messages();
    if (!box || !e.target) return;
    if (!(box.contains(e.target) || e.target === box)) return;
    var jump = openingAnother;
    openingAnother = false;
    // A different conversation opens at its newest message. Anything added to
    // the one being read follows only the reader who was already at the end of
    // it — measured in `beforeSwap`, because by now the list has grown.
    if (jump || following) toBottom(box);
  });

  // ── 3. the reply as it is written, and the audio as it is spoken ──
  //
  // The whole of the streaming client. It replaces htmx's request for this one
  // form and nothing else on the page: the same form, the same fields, the same
  // final markup appended into the same list by htmx itself.
  //
  // WHY THE LAST FRAME IS MARKUP. What grows during the turn is plain text,
  // because a half-written reply is half-written markdown and rendering it as
  // it arrives means rendering a table that has one row so far. The server
  // sends the finished exchange as the same fragment /admin/chat/fragment
  // answers with, and it is swapped in through htmx so the out-of-band rail
  // update and the player's own attributes work exactly as they always did.
  //
  // A TURN NO LONGER BELONGS TO THIS FETCH (detached-turns contract §3). It
  // used to: the server drove the whole turn off this connection, so a tablet
  // locking its screen ended a reply that had been running for twenty minutes
  // and nothing was written. The turn is now a task the server owns and this
  // fetch only watches it — which means three things here:
  //
  //   * losing the connection is not losing the turn, so a dropped stream is
  //     re-attached to instead of mourned;
  //   * a page opened while a reply is still being written attaches to it
  //     (`data-turn-running`, put on the form by the server);
  //   * STOPPING IS NOW A REAL THING TO DO, because closing the tab is not.
  //     See stopTheReply().
  var STREAM_URL = '/admin/chat/stream';
  var STOP_URL = '/admin/chat/stop';
  var streamable = !!(window.fetch && window.ReadableStream && window.AbortController
                      && window.TextDecoder && window.FormData);
  var inFlight = null;   // AbortController for the exchange being streamed
  var playing = null;    // the reply currently reading itself aloud
  var waitingToSpeak = []; // replies queued behind it — see speak()
  var stopToken = '';    // the running exchange, for the room's stop button

  // How many times a dropped stream will go back for the turn it was watching,
  // and how long it waits first. Bounded, because a turn that has finished
  // answers an attach with nothing — one wasted round trip is fine, a loop of
  // them is not. Four tries over roughly three seconds covers a tablet waking
  // up and a wifi hiccup, and gives up rather than hammering a core that is
  // genuinely gone.
  var ATTACH_TRIES = 4;
  var ATTACH_DELAY_MS = 700;
  var attachTries = 0;
  // The person pressed stop. Never re-attach after that: they asked for it to
  // end, and going back for it would be this file arguing with them.
  var stoppedByHand = false;

  // The reply bubble and its held text, at module scope rather than local to
  // `send()`. `box` used to be captured once at send time and `reply` once at
  // first write, and both were held for the life of the fetch — so a
  // whole-page swap mid-turn (chat_room.py returns the whole page for a
  // rename, a persona change, a group change, folding the rail — anything
  // that reaches `chat_room.py`) detached them and every token afterwards was
  // written into a node nobody could see. `messages()` already re-resolves
  // the list on every call; `reply` and its accumulated text are held here so
  // `htmx:afterSettle` can re-anchor them into whatever page just landed.
  var reply = null;       // the bubble currently being written into
  var replyText = '';     // its accumulated text — the only copy once a swap
                           // has detached the node holding the rest of it
  var replyAuthor = '';   // the name it was drawn under, so a re-anchored
                           // bubble reads the same as the one it replaces
  var streamWaiting = false; // mirrors the last call to waiting(), so a fresh
                              // #chat-waiting after a swap can be put back in
                              // the same state rather than defaulting to off

  // The collapsed "thinking" line and its held text — a visual indicator
  // that the model is actively thinking, expandable to see the reasoning
  // stream live. Held the same way `reply` and
  // `replyText` are, and for the same reason: a whole-page swap mid-turn
  // must not lose it. `thinking` is `null` until the first `thinking` frame
  // actually arrives — created lazily, not alongside `reply` at send time,
  // because a model that never reasons must draw none of this.
  //
  // THIS BUFFER ITSELF is never sent anywhere — it only ever grows a DOM node
  // this file owns and is thrown away, same as `replyText`, the moment the
  // finished exchange replaces it. That used to mean the reasoning was gone
  // on reload; it no longer does. The server keeps its own copy as the turn
  // finishes (`chat_streaming._record_reasoning`, 2026-09-02: the reasoning
  // is retained as additional context that can be provided back to the
  // model) and the rendered exchange this file swaps in
  // (`land()`, below) draws the same `.chat-thinking` markup from that copy —
  // so the line does not vanish, it changes which half of the page drew it.
  var thinking = null;     // the panel currently being written into, or null
  var thinkingText = '';   // its accumulated text — the only copy once a
                            // swap has detached the node holding the rest of it

  // The workspace-file cards row under the growing reply (workspace
  // contract §7), or null before the first one arrives. Created lazily on
  // the first `workspace_files` frame, the same reason `thinking` is: a
  // turn whose tools kept nothing must draw none of this. Carries
  // `data-streaming` like every other node this file draws while a
  // character is speaking, so `clearStreaming()` removes it the moment the
  // server's own rendering of the same reply lands — that rendering
  // includes the identical cards (chat_exchange_body.html includes the same
  // fragments/chat_workspace_cards.html this frame's own `html` is), so
  // nothing is lost, only redrawn from the one place that survives a
  // reload.
  var workspaceCards = null;

  function waiting(on) {
    streamWaiting = !!on;
    var line = document.getElementById('chat-waiting');
    if (!line) return;
    line.classList.toggle('htmx-request', streamWaiting);
  }

  // Audio the page owns rather than an element in the conversation. The
  // finished exchange carries the player people press to hear it again; this
  // is the one pass that happens while the words are still arriving, and an
  // <audio> control that vanished when the reply landed would be a second
  // player appearing and disappearing under every message.
  function stopPlaying() {
    waitingToSpeak = [];
    if (!playing) return;
    var going = playing;
    playing = null;
    try { going.pause(); going.src = ''; } catch (err) { /* already gone */ }
  }

  // ONE VOICE AT A TIME. A room can have several personas in it, each of which
  // starts speaking the moment its first sentence has ended — so without this,
  // the second talks over the first on the very first multi-persona turn. The
  // queue is here rather than on the server because this is where the playing
  // happens: the server has already put both streams somewhere to be fetched,
  // and holding one back there would only mean holding a socket open.
  //
  // TEXT IS NOT HELD BACK. Only the audio waits its turn — the words appear as
  // they are generated, which is the whole point of the streaming path.
  function speak(url) {
    if (!url || url.indexOf('/admin/chat/live/') !== 0) return;
    if (playing) { waitingToSpeak.push(url); return; }
    play(url);
  }

  function play(url) {
    var sound = new Audio(url);
    playing = sound;
    // Whatever ends this one — played through, or an engine that never
    // produced anything — the next voice gets its turn. A queue that only
    // advanced on success would go silent for the rest of the exchange the
    // first time a persona had no voice.
    sound.addEventListener('ended', next);
    sound.addEventListener('error', next);
    function next() {
      if (playing !== sound) return;
      playing = null;
      var following = waitingToSpeak.shift();
      if (following) play(following);
    }
    // A refusal here costs the audio and nothing else — the reply is on the
    // screen either way, which is the rule the whole voice path is built on.
    var started = sound.play();
    if (started && started.catch) started.catch(function () { next(); });
  }

  function bubble(box, who) {
    // The reply bubble, drawn to match fragments/chat_exchange_body.html. It
    // is plain text until the server's own rendering replaces it.
    //
    //  is the character now speaking, and it is only passed for the
    // second and later voices in a room: the first reply's name arrives with
    // the server's rendering, and drawing a name over the very first bubble
    // would put a header on a single-persona reply that never had one. The
    // server sends the name; this file never composes one, because the
    // parentheses rule that tells a person from a persona lives in chat.py.
    if (who) {
      var name = document.createElement('p');
      name.className = 'chat-said';
      name.style.alignSelf = 'flex-start';
      name.setAttribute('data-streaming', '');
      name.textContent = who;
      box.appendChild(name);
    }
    var el = document.createElement('div');
    el.setAttribute('data-streaming', '');
    el.style.cssText =
      'align-self:flex-start; max-width:85%; margin:0; padding:var(--space-3) var(--space-4);' +
      'border-radius:var(--radius-md); background:var(--color-surface); font-size:14px;' +
      'line-height:1.55; white-space:pre-wrap';
    box.appendChild(el);
    return el;
  }

  // Copied from `_controls.html`'s own `icon('spinner')`/`icon('down')` paths
  // rather than fetched, the same way `REMOVE_ICON_SVG` further down this file
  // is: this markup is built entirely by script, with no server-rendered
  // element to read a class or a `<use>` reference off. `spin` is
  // nocturne.css's own rotation class, reused rather than duplicated, so its
  // one `prefers-reduced-motion` guard covers this spinner too.
  var THINKING_SPINNER_SVG =
    '<svg class="icon spin" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.5A6.5 6.5 0 1 1 1.5 8"/></svg>';
  var THINKING_CHEVRON_SVG =
    '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6l5 5 5-5"/></svg>';

  function thinkBubble(box, before) {
    // The collapsed line above a growing reply, filled in live as reasoning
    // arrives — see the module-scope comment on `thinking`/`thinkingText`
    // above for why this exists and why it is created lazily rather than
    // alongside `reply`. A native <details>/<summary>, the same pattern
    // fragments/chat_exchange_body.html's own `.msg-bar` uses for exactly the
    // same reason: the chevron needs no script, only the panel's contents do.
    //
    // `before` is `reply` (or `null`) — the node this turn's answer is being
    // written into, when one already exists. Inserted with `insertBefore`
    // rather than relying on call order, so this lands above the reply
    // whether the reply bubble was created a moment ago or an hour ago: a
    // live DOM reference does not care which.
    var el = document.createElement('details');
    el.className = 'chat-thinking';
    el.setAttribute('data-streaming', '');
    var summary = document.createElement('summary');
    summary.className = 'chat-thinking-summary';
    summary.innerHTML =
      '<span class="chat-thinking-spinner" aria-hidden="true">' + THINKING_SPINNER_SVG + '</span>' +
      '<span>Thinking…</span><span style="flex:1"></span>' +
      '<span class="chat-thinking-chevron" aria-hidden="true">' + THINKING_CHEVRON_SVG + '</span>';
    el.appendChild(summary);
    var body = document.createElement('div');
    body.className = 'chat-thinking-body';
    el.appendChild(body);
    if (before && before.parentNode === box) box.insertBefore(el, before);
    else box.appendChild(el);
    return body;
  }

  // Everything this file drew while a character was speaking. Removed when the
  // server's rendering of the same reply lands, so the words are never on the
  // screen twice.
  function clearStreaming() {
    var stale = document.querySelectorAll('[data-streaming]');
    for (var i = 0; i < stale.length; i++) stale[i].remove();
  }

  // §5's button — STOP THE ROOM, which is not the same thing as stopping the
  // reply. Lit only while a room is talking: shown when the server says an
  // exchange with more than one persona has started, hidden the moment it
  // ends. A single persona never sends a token, so this button never appears
  // for the conversation most people are having — which is exactly why the
  // composer's own send/stop control had to exist (§4a).
  function stopButton(on) {
    var found = document.querySelectorAll('[data-chat-stop]');
    for (var i = 0; i < found.length; i++) found[i].hidden = !on;
  }

  // Which thread the next message — or the next attach — belongs to. Read
  // fresh every time from the composer's own hidden field, which the server
  // keeps correct through fragments/chat_markers.html: reading it once and
  // holding it would be the same class of mistake as holding `box`.
  function conversationNow() {
    var field = document.getElementById('chat-conversation');
    return (field && field.value) || '';
  }

  // ── the one send/stop button — contract §4a ──
  //
  // One control, bottom-right inside the composer, in the two states read off
  // screenshots of the client this design is copying:
  //
  //   turn running, box EMPTY     dark circle, white square  — stop
  //   turn running, box HAS TEXT  orange circle, up arrow    — send
  //
  // So it is NOT simply "running means stop". With a reply in flight and
  // something typed, that client offers send — the button follows what the
  // person can usefully do next rather than what the system is doing. Idle is
  // the same send, which is what it has always been.
  //
  // WHAT A SEND DOES DURING A RUNNING TURN IS SETTLED NOW and is not this
  // function's business: it queues (composer.md §3, and `queueMessage` below).
  // The button's own two states are unchanged by that — a message waiting to
  // go is not something to stop, and the reply being written is.
  function composerButton() {
    var button = document.getElementById('chat-send');
    if (!button) return;
    var box = document.getElementById('chat-input');
    var typed = !!(box && box.value && box.value.trim());
    var stopping = !!inFlight && !typed;
    if (stopping) button.setAttribute('data-stopping', '');
    else button.removeAttribute('data-stopping');
    var says = stopping ? 'Stop this reply' : 'Send';
    button.setAttribute('aria-label', says);
    button.setAttribute('title', says);
    var arrow = button.querySelector('[data-send-glyph]');
    var square = button.querySelector('[data-stop-glyph]');
    if (arrow) arrow.hidden = stopping;
    if (square) square.hidden = !stopping;
  }

  // §4a's stop: halt the answer being written. The missing sibling of the
  // room's stop above, and now the only way to end a turn somebody has walked
  // away from — closing the tab does not, which is the entire point of §3.
  //
  // THE STREAM IS NOT ABORTED. Aborting is detaching, and detaching is not
  // stopping (§5 rule 5): the turn would go on running with nobody watching
  // it, which is the opposite of what was asked for. The server ends the turn
  // and this same stream carries the last frame.
  //
  // The audio is cut here, immediately, exactly as the room's stop cuts it:
  // pressing stop and then listening to another twenty seconds of speech is
  // not stopping.
  function stopTheReply() {
    if (!inFlight) return;
    stoppedByHand = true;
    stopPlaying();
    // MINE — the contract settles what a send does during a turn and says
    // nothing about what a stop does to a message waiting behind one. A queued
    // message firing seconds after somebody asked for quiet is the opposite of
    // stop, so stop takes it out of the queue. It is not lost: `takeBackQueued`
    // puts the words back in the box, and the box is empty whenever this
    // button is offering stop at all.
    takeBackQueued();
    var body = new FormData();
    body.append('conversation', conversationNow());
    // A room's own stop rides along on the same post. Pressing this while
    // several characters are talking should end the sentence being written
    // AND not ask the next one; they are two different stops and this is the
    // one place a person expects both.
    if (stopToken) {
      body.append('token', stopToken);
      stopToken = '';
      stopButton(false);
    }
    if (window.fetch) {
      fetch(STOP_URL, {
        method: 'POST', body: body, credentials: 'same-origin'
      }).catch(function () {});
    }
  }

  function land(box, html) {
    // Through htmx, not innerHTML: the response carries an out-of-band swap for
    // the conversation rail and hx-* attributes on the player, and both are
    // htmx's to process. The fallback is only for an htmx too old to expose
    // swap() — the exchange still appears, without the rail catching up.
    if (window.htmx && window.htmx.swap) {
      window.htmx.swap(box, html, { swapStyle: 'beforeend' });
      return;
    }
    box.insertAdjacentHTML('beforeend', html);
    if (window.htmx && window.htmx.process) window.htmx.process(box);
  }

  function trouble(box, text) {
    var el = document.createElement('p');
    el.className = 'banner warn';
    el.setAttribute('role', 'status');
    el.style.cssText = 'align-self:flex-start; max-width:85%; margin:0';
    el.textContent = text;
    box.appendChild(el);
  }

  // One SSE frame: `event:` and `data:` lines, ended by a blank line. Written
  // out rather than reached for, because the only parser the browser ships is
  // inside EventSource and EventSource cannot POST.
  function frames(text, onFrame) {
    var parts = text.split('\n\n');
    var rest = parts.pop();
    for (var i = 0; i < parts.length; i++) {
      var name = '';
      var data = '';
      var lines = parts[i].split('\n');
      for (var j = 0; j < lines.length; j++) {
        var line = lines[j];
        if (line.indexOf('event:') === 0) name = line.slice(6).trim();
        else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
      }
      if (!name) continue;
      var payload = {};
      if (data) { try { payload = JSON.parse(data); } catch (err) { continue; } }
      onFrame(name, payload);
    }
    return rest;
  }

  function bodyFor(form) {
    // What htmx collected before the box was cleared, when we have it — the
    // live form is empty by now. Falling back to the form keeps a send working
    // if this ever runs without `configRequest` having fired first.
    if (!pendingParams) return new FormData(form);
    var body = new FormData();
    for (var key in pendingParams) {
      if (Object.prototype.hasOwnProperty.call(pendingParams, key)) {
        // Attachments are appended fresh, below, from the live file input —
        // never from what htmx captured here. `configRequest` never touches
        // that input (only the message box, which is the whole reason
        // `pendingParams` exists), so it still holds exactly what was chosen
        // or pasted, and htmx's own parameter collection is not something
        // this file relies on to carry several files under one name.
        if (key === ATTACH_FIELD) continue;
        body.append(key, pendingParams[key]);
      }
    }
    pendingParams = null;
    var picked = document.getElementById(ATTACH_INPUT_ID);
    if (picked && picked.files) {
      for (var i = 0; i < picked.files.length; i++) body.append(ATTACH_FIELD, picked.files[i]);
    }
    return body;
  }

  // ── a message that waits its turn — composer.md contract §3 ──
  //
  // 2026-09-02: send should allow adding to and queueing a message without
  // aborting a currently running session — stop is a separate, deliberate
  // action.
  //
  // THIS IS THE BEHAVIOUR CHANGE. `send()` used to abort the fetch and let the
  // server stop-and-replace the turn — "a second send cancels the first" — and
  // that is exactly the behaviour rejected. A second send now touches
  // the running turn not at all. Only stop stops.
  //
  // ONE MESSAGE WAITS. That is the contract's own depth and it is stated there
  // as the minimum that satisfies the requirement above. A send arriving while one
  // is already waiting is refused rather than replacing it — the words go
  // straight back into the box they came from, so nothing typed is ever thrown
  // away here, by this path or by the take-back below.
  var queued = null;      // { body: FormData, text: string, files: [File] }
  var QUEUE_NOTE_MS = 3000;

  function renderQueued() {
    var rows = document.querySelectorAll('[data-chat-queued]');
    for (var i = 0; i < rows.length; i++) {
      // `hidden`, never a style: the stylesheet spells `.chat-queued[hidden]`
      // out precisely so the attribute wins, which is the v0.13.11 defect this
      // row would otherwise repeat.
      rows[i].hidden = !queued;
      var said = rows[i].querySelector('[data-chat-queued-text]');
      if (said) said.textContent = queued ? queued.text : '';
    }
  }

  // Put a message behind the running turn. False when one is already waiting.
  function queueMessage(body, text, files) {
    if (queued) return false;
    queued = { body: body, text: text, files: files || [] };
    renderQueued();
    composerButton();
    return true;
  }

  // Everything a queued message was carrying, back where it was typed. Used by
  // the take-back control, by a stop, and by a send this file could not accept
  // — three ways of not sending something, and none of them may lose it.
  function backToComposer(text, files) {
    var input = document.getElementById('chat-input');
    if (input && text) {
      input.value = input.value ? text + '\n' + input.value : text;
      grow(input);
      input.focus();
    }
    for (var i = 0; i < (files || []).length; i++) pendingFiles.push(files[i]);
    if (files && files.length) { syncAttachmentInput(); renderAttachTiles(); }
    composerButton();
  }

  function takeBackQueued() {
    if (!queued) return;
    var held = queued;
    queued = null;
    renderQueued();
    backToComposer(held.text, held.files);
  }

  // The sentence for a send that arrived with one already waiting. In the
  // markup, revealed and hidden again, so the words live in the template with
  // every other thing this surface says.
  function queueFullNote() {
    var notes = document.querySelectorAll('[data-chat-queue-full]');
    for (var i = 0; i < notes.length; i++) {
      var note = notes[i];
      note.hidden = false;
      window.clearTimeout(note.queueNoteTimer);
      note.queueNoteTimer = window.setTimeout(function (it) {
        return function () { it.hidden = true; };
      }(note), QUEUE_NOTE_MS);
    }
  }

  // The running turn has finished, so whatever was waiting goes now — as an
  // ordinary turn, drawn the way any other message is.
  function sendQueued() {
    if (!queued || inFlight) return;
    var going = queued;
    queued = null;
    renderQueued();
    attachTries = 0;
    stoppedByHand = false;
    var box = messages();
    var form = document.getElementById('chat-form');
    drawQuestion(box, going.text, going.files, form && form.getAttribute('data-author'));
    if (box) toBottom(box);
    openStream(going.body, false);
  }

  function send(form) {
    var box = messages();
    if (!box) return false;

    // Built before the pending tiles are cleared: `bodyFor` reads the file
    // input's own `.files` directly, and a `FormData` holds its own copies of
    // whatever it was given — clearing the input and the chip row afterwards
    // does not reach back into a body already handed to `fetch`.
    var carrying = pendingFiles.slice();
    var body = bodyFor(form);
    var text = (body.get && (body.get('message') || '').toString().trim()) || '';
    resetPendingAttachments();

    if (inFlight) {
      // §3. The reply being written is not touched — no abort, nothing posted
      // — and the question this file drew a moment ago comes down again,
      // because it has not been said yet. It goes back up when it goes.
      clearPending();
      if (!queueMessage(body, text, carrying)) {
        backToComposer(text, carrying);
        queueFullNote();
      }
      return true;
    }

    stopPlaying();
    // A fresh turn, so the attach budget starts over and an earlier stop is
    // forgotten — this is a new thing to watch, not the old one coming back.
    attachTries = 0;
    stoppedByHand = false;
    // Unconditional, and the one place that still is: pressing Send is asking
    // to see what happens next.
    toBottom(box);
    return openStream(body, false);
  }

  // ── 8. resend — composer.md contract §7, detached-turns.md §4b ──
  //
  // Any message already sent needs to be able to be resent by clicking a
  // resend button underneath it. A NEW TURN, never an
  // edit: the transcript keeps what was said the first time and gains the
  // second asking.
  //
  // The control is a plain form in the message bar and works with this file
  // never loaded (fragments/chat_exchange_body.html). What this adds is the
  // same thing it adds to the composer: the reply arrives as it is written
  // instead of the page reloading when it is finished — and §3, which is not
  // optional here. Resending during a running turn queues, because "only stop
  // stops" is about turns, not about which control started one.
  //
  // The marker comes from `conversationNow()` rather than from the form's own
  // hidden field: that field is correct when the messages were rendered, and
  // `conversationNow()` is correct now. They agree except in the one case that
  // matters — a conversation that gained its marker mid-stream.
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var form = e.target.closest('form[data-resend]');
    if (!form || !streamable) return;
    // The bar is a <summary>, so the default action here is opening the
    // disclosure as well as submitting. Both are suppressed together, exactly
    // as the copy button beside it suppresses both.
    e.preventDefault();
    var said = form.querySelector('input[name="message"]');
    var text = ((said && said.value) || '').trim();
    if (!text) return;
    var marker = form.querySelector('input[name="conversation"]');
    var where = conversationNow() || (marker && marker.value) || '';
    // A resend that cannot say which thread it belongs to does not guess. An
    // empty marker does not mean "this conversation" to the server — it means
    // "make one", so the message would be replayed into a brand new thread
    // instead of the one it was said in, and the resend button would be the
    // thing that started it. The form is only rendered with a marker on it
    // (chat_exchange_body.html guards on exactly that), so this refusal
    // cannot fire from the shipped page; it is here so that the one control
    // whose whole job is "ask this thread again" can never do the opposite.
    if (!where) return;
    var body = new FormData();
    body.append('conversation', where);
    body.append('message', text);

    if (inFlight) {
      if (!queueMessage(body, text, [])) queueFullNote();
      return;
    }
    stopPlaying();
    attachTries = 0;
    stoppedByHand = false;
    var box = messages();
    var composer = document.getElementById('chat-form');
    drawQuestion(box, text, [], composer && composer.getAttribute('data-author'));
    if (box) toBottom(box);
    openStream(body, false);
  });

  // Go back for a turn this file was watching and lost sight of.
  //
  // AN EMPTY MESSAGE IS THE ATTACH, and it is not a new field: an empty
  // message has always meant "there is nothing to send", and with a turn
  // already running for this conversation there is nothing to send *and*
  // something to watch. The server reads it exactly that way. That is also
  // what makes rule 2 hold — attaching twice must not run the turn twice —
  // because nothing this file posts to re-attach carries a message that could
  // start one.
  //
  // A turn that has already finished answers this with nothing at all, and
  // openStream's `attaching` flag is what stops that nothing being drawn as a
  // refusal in the middle of the conversation.
  function attachToTurn() {
    if (inFlight) return;
    var marker = conversationNow();
    if (!marker) return;
    var body = new FormData();
    body.append('conversation', marker);
    body.append('message', '');
    openStream(body, true);
  }

  // The server said a reply is still being written in this thread. Attach to
  // it, once per render — §6: there is no control here and nothing to press,
  // it is simply not broken any more.
  function attachIfTurnRunning() {
    if (inFlight || !streamable) return;
    var form = document.getElementById('chat-form');
    if (!form || !form.hasAttribute('data-turn-running')) return;
    form.removeAttribute('data-turn-running');
    attachTries = 0;
    stoppedByHand = false;
    attachToTurn();
  }

  // One turn, watched. `attaching` says this fetch did not start the turn it
  // is reading — it went back for one that was already running — which changes
  // exactly two things: the words already produced are replayed, so whatever
  // this file drew for them is cleared first; and a stream that carries no
  // frame but `done` is a turn that had already finished, whose empty answer
  // must not be drawn into the conversation.
  function openStream(body, attaching) {
    var box = messages();
    if (!box) return false;

    var controller = new AbortController();
    inFlight = controller;
    // `reply`/`replyText`/`replyAuthor` are module-scoped (see their
    // declaration above) precisely so `htmx:afterSettle` can reach them if a
    // whole-page swap lands mid-turn. Every write below goes through them
    // rather than a local, and `box` itself is never held past this line —
    // everywhere in the fetch chain below asks `messages()` fresh instead, so
    // a swap in the middle of a turn hands the next write the page that
    // actually exists rather than the one that was current at Send.
    reply = attaching ? null : bubble(box, '');
    replyText = '';
    replyAuthor = '';
    // No thinking panel yet — see `thinking`'s own declaration for why this
    // is created lazily, on the first `thinking` frame, rather than here.
    thinking = null;
    thinkingText = '';
    workspaceCards = null;
    var settled = false;
    var carried = false; // any frame other than `done` — see `attaching`
    var lost = false;
    var leaving = false; // the session went and this page is being replaced
    waiting(true);
    composerButton();

    fetch(STREAM_URL, {
      method: 'POST',
      body: body,
      headers: { 'Accept': 'text/event-stream' },
      credentials: 'same-origin',
      signal: controller.signal
    }).then(function (response) {
      // Not an event stream means this was not the answer to a turn at all —
      // the session went and the sign-in page came back instead. Reloading
      // lands on it. A banner would be this screen guessing at what happened.
      var kind = response.headers.get('content-type') || '';
      if (response.ok && kind.indexOf('text/event-stream') !== 0) {
        settled = true;
        // Not a finished turn, so nothing waiting behind it may be released —
        // this page is being replaced by the sign-in screen.
        leaving = true;
        window.location.reload();
        return;
      }
      if (!response.ok || !response.body) throw new Error('stream refused');
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffered = '';

      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) return;
          buffered += decoder.decode(chunk.value, { stream: true });
          buffered = frames(buffered, function (name, payload) {
            // Anything but `done` means there was a turn here to watch. See
            // `attaching`: a stream that carries only `done` was answered by a
            // core with no running turn, and drawing its answer would put a
            // refusal card under a conversation nobody refused.
            if (name !== 'done') {
              if (attaching && !carried) {
                // The replay is about to redraw the whole reply, including the
                // part this file already drew before it lost the connection.
                // Cleared HERE, on the first frame that actually arrives, and
                // not when the attach was sent: a turn that had already
                // finished answers an attach with nothing at all, and wiping
                // the screen for that would throw away the only copy of the
                // words somebody was reading.
                appendKeepingPlace(messages(), clearStreaming);
                reply = null;
                replyText = '';
                replyAuthor = '';
                thinking = null;
                thinkingText = '';
                workspaceCards = null;
              }
              carried = true;
            }
            if (name === 'markers') {
              // The turn now knows which conversation it is running in — the
              // first thing this stream says, before a word of the reply.
              // `payload.html` is `fragments/chat_markers.html`, the whole set
              // of hx-swap-oob controls that name the open thread (chat.py's
              // own comment on that file explains why it is all of them and
              // not just the composer's field). Through `land()`, not
              // innerHTML, because every element in it is `hx-swap-oob` and
              // htmx is what moves an oob element to where its id already is
              // on the page — there is no non-oob content here, so nothing is
              // added to `#chat-messages` itself.
              //
              // THIS IS THE FIX. Until this frame existed, every one of those
              // controls carried the instant the *page* was opened at for the
              // whole streaming window — a new chat's page-open instant names
              // no conversation at all — so pressing Delete, the persona
              // picker, Save or the rail's fold while a reply was still
              // arriving posted a marker naming nothing, which opened a fresh
              // conversation instead of reaching the one still running:
              // interacting with the page while waiting for a response landed
              // on a new chat window.
              var markersBox = messages();
              if (markersBox) land(markersBox, payload.html || '');
            } else if (name === 'delta') {
              waiting(false);
              appendKeepingPlace(messages(), function () {
                if (!reply) reply = bubble(messages(), replyAuthor);
                // textContent: this is the model's own words, and they are not
                // markup until the server says they are. Held in `replyText`
                // too — the only copy that survives a swap detaching `reply`.
                replyText += payload.text || '';
                reply.textContent = replyText;
              });
            } else if (name === 'thinking') {
              // The model's own reasoning, arriving on its own channel while
              // `content` may still be empty — a long paste produced
              // eighteen minutes of apparent nothing while `n_gen` climbed past
              // twelve thousand tokens, almost all of it this. Drawn ONLY
              // here, live, into this file's own bubble — it never joins
              // `replyText` and never reaches `land()` directly, so it cannot
              // be mistaken for the answer or end up spoken. That used to
              // also mean it could not survive a reload; it now does, kept on
              // the server under the reply's own id and drawn back by the
              // finished exchange's own rendered HTML the next time this
              // conversation is opened — a second copy this file never has to
              // manage, because by the time that HTML lands `clearStreaming()`
              // has already removed this bubble. Created on the first word of
              // it, not before — a model that never reasons must draw none of
              // this.
              var text = payload.text || '';
              if (text) {
                appendKeepingPlace(messages(), function () {
                  if (!thinking) thinking = thinkBubble(messages(), reply);
                  thinkingText += text;
                  thinking.textContent = thinkingText;
                });
              }
            } else if (name === 'speech') {
              speak(payload.url);
            } else if (name === 'notice') {
              waiting(false);
              appendKeepingPlace(messages(), function () {
                if (!reply) reply = bubble(messages(), replyAuthor);
                replyText += payload.text || '';
                reply.textContent = replyText;
              });
            } else if (name === 'exchange') {
              // More than one persona is in this room, so there is something
              // for a stop button to stop.
              stopToken = payload.token || '';
              stopButton(!!stopToken);
            } else if (name === 'turn') {
              // The next character has begun. A bubble of its own, under its
              // own name, so a room reads as a room rather than as one very
              // long reply.
              waiting(true);
              appendKeepingPlace(messages(), function () {
                replyAuthor = payload.author || '';
                replyText = '';
                reply = bubble(messages(), replyAuthor);
                // The next character's own thinking, if any — not this one's.
                thinking = null;
                thinkingText = '';
                // Likewise the next character's own workspace cards, if any.
                workspaceCards = null;
              });
            } else if (name === 'workspace_files') {
              // A tool call this turn just made left a file behind
              // (workspace contract §7) — a card under the growing reply,
              // the same `attach-card` markup the finished exchange draws
              // (fragments/chat_workspace_cards.html, rendered server-side
              // either way so the two are pixel for pixel the same). Not
              // `land()`: that swaps a whole boundary by id, and this is one
              // or more small tiles appended to a row that keeps growing as
              // more tool calls finish.
              var workspaceHtml = payload.html || '';
              if (workspaceHtml) {
                appendKeepingPlace(messages(), function () {
                  if (!reply) reply = bubble(messages(), replyAuthor);
                  if (!workspaceCards) {
                    workspaceCards = document.createElement('div');
                    workspaceCards.className = 'attach-sent-row';
                    workspaceCards.style.alignSelf = 'flex-start';
                    workspaceCards.setAttribute('data-streaming', '');
                    // No bare `else` here: the dispatch must stay a chain of
                    // named frames so an older client can ignore one it does
                    // not know (test_chat_streaming's older-client check).
                    var anchored = reply && reply.parentNode === messages();
                    if (anchored) reply.insertAdjacentElement('afterend', workspaceCards);
                    if (!anchored) messages().appendChild(workspaceCards);
                  }
                  workspaceCards.insertAdjacentHTML('beforeend', workspaceHtml);
                });
              }
            } else if (name === 'reply') {
              // One character has finished and another follows. Its rendered
              // exchange lands now rather than at the end of the room's
              // conversation, so the markdown, the player and the footer
              // appear under the character that said it.
              waiting(false);
              // Measured before any of it: clearing the growing text shrinks
              // the list and landing the rendered exchange grows it again.
              appendKeepingPlace(messages(), function () {
                clearPending();
                clearStreaming();
                reply = null;
                replyText = '';
                replyAuthor = '';
                // `clearStreaming()` already removed the thinking panel and
                // the workspace-cards row — both carry `data-streaming` like
                // the bubble they sat above — so this only lets go of
                // references to nodes that are gone.
                thinking = null;
                thinkingText = '';
                workspaceCards = null;
                land(messages(), payload.html || '');
              });
            } else if (name === 'done') {
              settled = true;
              waiting(false);
              stopToken = '';
              stopButton(false);
              appendKeepingPlace(messages(), function () {
                clearPending();
                clearStreaming();
                reply = null;
                replyText = '';
                replyAuthor = '';
                thinking = null;
                thinkingText = '';
                workspaceCards = null;
                if (attaching && !carried) return;
                land(messages(), payload.html || '');
              });
            }
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      // NOT SAID YET. The connection went; the turn did not — it belongs to
      // the server now, and this file's first move is to go back for it rather
      // than to announce a loss that has probably not happened. The sentence
      // is still there, below, for when the attempts run out.
      if (!settled) lost = true;
    }).then(function () {
      // Whether this fetch is still the one anybody cares about. `reply` is
      // shared with the swap re-anchor in `htmx:afterSettle` now, not local
      // to this call, so a superseded fetch's own cleanup — this Send was
      // cancelled because a second one started — must not reach in and
      // delete the *new* turn's bubble just because this stale promise
      // settles after it.
      var isCurrent = inFlight === controller;
      if (isCurrent) inFlight = null;
      // The same rule for the indicators, which is older than the re-anchor
      // and was wrong before it. `send()` aborts the previous turn and then
      // sets `inFlight` and `waiting(true)`, all synchronously; the aborted
      // fetch's rejection is a microtask, so this block used to run *after*
      // the new turn had started and switched the waiting line off, hid the
      // stop button and dropped the new turn's stop token. It healed itself
      // when the first frame arrived -- eight to fourteen seconds later on a
      // slow host, which reads as the assistant having stopped responding.
      if (isCurrent) {
        waiting(false);
        stopToken = '';
        stopButton(false);
        composerButton();
      }
      // THE TURN FINISHED, SO WHATEVER WAS WAITING GOES NOW (contract §3).
      // Only on a turn that actually reached its `done` frame: a dropped
      // stream is re-attached to below, and releasing the queue into a turn
      // that is still running would be the two-turns-at-once this whole
      // change exists to prevent.
      if (isCurrent && settled && !leaving) sendQueued();
      if (settled) return;
      // THE STREAM ENDED WITHOUT `done`, WHICH IS NO LONGER THE END OF THE
      // TURN. The tablet slept, the wifi dropped, the browser froze the page —
      // and the reply is still being written on the server, which is the whole
      // of what §3 changed. So go back for it, a bounded number of times,
      // instead of drawing a half-answer and a apology over a turn that is
      // still working.
      //
      // Never after a stop: the person asked for it to end.
      if (isCurrent && !stoppedByHand && attachTries < ATTACH_TRIES) {
        attachTries += 1;
        window.setTimeout(attachToTurn, ATTACH_DELAY_MS);
        return;
      }
      // Out of tries. The turn is genuinely out of reach and the words that
      // arrived are still on the screen; saying so beats leaving a half-reply
      // that looks like the whole one.
      if (lost) {
        trouble(messages() || box, 'The connection to the assistant was lost before the reply finished.');
      }
      // Nothing arrived at all, so nothing happened: the question must not be
      // left sitting there looking sent. Words that DID arrive are left where
      // they are — the turn really ran, and the transcript has it.
      if (isCurrent && reply && !reply.textContent) { reply.remove(); clearPending(); }
    });
    return true;
  }

  // htmx is asked to stand down for this one form, and only when everything
  // this needs exists. Cancelling `htmx:beforeRequest` leaves `configRequest`
  // — which drew the question and cleared the box — already done.
  document.addEventListener('htmx:beforeRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || form.id !== 'chat-form' || !streamable) return;
    if (send(form)) e.preventDefault();
  });

  // Leaving the page stops the speech. It does NOT abort the turn.
  //
  // It used to, and on Android that lost the reply: switching applications away
  // from the browser window interrupted the conversation. `pagehide` is not
  // "the reader has gone" on mobile — a
  // browser being backgrounded fires it too, usually with `persisted === true`
  // because the page is going into the back/forward cache and is expected to
  // come back. A turn runs one to two minutes, so a glance at
  // another app threw one away.
  //
  // Dropping the abort rather than guarding it on `event.persisted`, because
  // the abort was never the mechanism. The server lets go of the turn and the
  // model socket when the connection drops, from the generator's `finally` and
  // from the response's background task — see `_TurnHolding` in chat.py, which
  // exists precisely because that is the ordinary way a streamed reply ends. A
  // closed tab tears the socket down whatever this file does. So the abort
  // bought nothing the socket close does not, and cost a turn every time a
  // lifecycle event was read as "gone" when it meant "hidden". A guard would
  // narrow that window; removing the line closes it.
  //
  // It no longer has to promise anything about the turn at all. The browser
  // freezing the page and cancelling this fetch used to lose the reply; it now
  // loses only the view of it (detached-turns contract §3), and the page that
  // comes back attaches to the turn still running — `attachIfTurnRunning`, or
  // the dropped-stream path in openStream's tail — closing the client entirely
  // and coming back finds it right where it was left.
  //
  // The speech still stops, and that is deliberate: audio from a tab somebody
  // has switched away from is startling. `stopPlaying` empties the queue and
  // clears the playing sound, which leaves both back at their starting state —
  // so a page restored from the cache is ready to speak the next reply rather
  // than half torn down.
  window.addEventListener('pagehide', function () {
    stopPlaying();
  });

  // ── attachments — attachments.md contract §5/§6a ──
  //
  // A chip cannot exist without this file: a thumbnail is a read of the file
  // it previews, and a truncated-name badge over a pasted block is a read of
  // the paste. So this is the one part of the composer that genuinely needs a
  // script — the file input above it does not (a plain <input type="file">
  // sends whatever was chosen with no code behind it at all), and this
  // section says so rather than pretending it drew something it did not.
  //
  // BYTES ARE HELD HERE, NOT ON THE SERVER, UNTIL SEND. Nothing this file
  // adds to `pendingFiles` is uploaded before the form actually posts, which
  // is what makes the chip's `X` a plain, complete cancellation: there is
  // nothing stored yet to delete, only an array entry to drop and a tile to
  // remove. The `X` is not just a visible thing that lays over
  // the chip — it removes that one attachment from the pending turn, and
  // this is how.
  var ATTACH_INPUT_ID = 'chat-attachments-input';
  var ATTACH_FIELD = 'attachments';
  var pendingFiles = []; // File objects, in the order added — the pending turn's own list

  // The same `x` path `_controls.html`'s `icon('x')` draws, copied rather
  // than fetched: this button is built entirely by script and has no
  // server-rendered markup to read a class or a `<use>` reference off.
  var REMOVE_ICON_SVG =
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M3 3l10 10M13 3L3 13"/></svg>';

  // The file input's own `.files` is read-only except by wholesale
  // replacement — there is no "remove the third one" on a FileList — so
  // every mutation to `pendingFiles` rebuilds it through a fresh
  // `DataTransfer`. Setting `.files` this way does not fire `change`, so this
  // never re-enters the listener that reads a user's own picks, below.
  function syncAttachmentInput() {
    var input = document.getElementById(ATTACH_INPUT_ID);
    if (!input) return;
    var carried = new DataTransfer();
    for (var i = 0; i < pendingFiles.length; i++) carried.items.add(pendingFiles[i]);
    input.files = carried.files;
  }

  function attachBadge(file) {
    var name = file.name || '';
    var dot = name.lastIndexOf('.');
    var ext = dot > -1 ? name.slice(dot + 1) : '';
    return (ext || 'file').toUpperCase().slice(0, 4);
  }

  function attachTile(file, index) {
    var tile = document.createElement('div');
    tile.className = 'attach-tile';
    if (file.type && file.type.indexOf('image/') === 0) {
      var img = document.createElement('img');
      var url = URL.createObjectURL(file);
      img.src = url;
      img.alt = '';
      // The object URL is only needed long enough to paint the thumbnail;
      // holding it past that is a leak for every image added and removed
      // over a long conversation.
      img.addEventListener('load', function () { URL.revokeObjectURL(url); });
      tile.appendChild(img);
    } else {
      var fallback = document.createElement('div');
      fallback.className = 'attach-fallback';
      var badge = document.createElement('span');
      badge.className = 'attach-badge';
      badge.textContent = attachBadge(file);
      var name = document.createElement('span');
      name.className = 'attach-name';
      name.textContent = file.name || 'attachment';
      fallback.appendChild(badge);
      fallback.appendChild(name);
      tile.appendChild(fallback);
    }
    // The circular `X`, overlapping the tile's own corner (contract §6a). A
    // real control: removing this one entry, leaving the others and the
    // typed text alone, and it carries what it removes rather than a bare
    // "x" so a screen reader says something a person typed did not.
    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'attach-remove';
    remove.setAttribute('aria-label', 'Remove ' + (file.name || 'this attachment'));
    remove.innerHTML = REMOVE_ICON_SVG;
    remove.addEventListener('click', function () {
      pendingFiles.splice(index, 1);
      syncAttachmentInput();
      renderAttachTiles();
    });
    tile.appendChild(remove);
    return tile;
  }

  // The same tile without its remove control, for a message already sent.
  // Deliberately a second function rather than a flag on `attachTile`: the
  // two differ only in whether cancelling is still possible, and that is
  // exactly the thing worth being unable to get wrong by passing `false`.
  function attachSummaryTile(file) {
    var tile = attachTile(file, -1);
    var remove = tile.querySelector('.attach-remove');
    if (remove) remove.remove();
    return tile;
  }

  function renderAttachTiles() {
    var box = document.getElementById('chat-attach-tiles');
    if (!box) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    for (var i = 0; i < pendingFiles.length; i++) box.appendChild(attachTile(pendingFiles[i], i));
  }

  function addPendingFile(file) {
    pendingFiles.push(file);
    syncAttachmentInput();
    renderAttachTiles();
  }

  function resetPendingAttachments() {
    pendingFiles = [];
    var input = document.getElementById(ATTACH_INPUT_ID);
    if (input) input.value = '';
    renderAttachTiles();
  }

  // A file chosen through the native picker. Added rather than replacing —
  // the input can be opened more than once before Send — and immediately
  // re-synced onto the input itself so a chip removed afterwards actually
  // changes what would be uploaded.
  //
  // THREE INPUTS, ONE LIST. The `+` sheet draws Camera, Photos and Files
  // (composer.md §5), which are three ordinary file inputs differing only in
  // what they accept — that is what makes each of them work with no script at
  // all, and it is why this reads `[data-attach-input]` rather than one id.
  // Whichever was used, the files land in `pendingFiles` and are synced onto
  // the ONE canonical input, and the other two are emptied so a browser
  // posting the form itself cannot send the same picture twice.
  document.addEventListener('change', function (e) {
    var input = e.target;
    if (!input || !input.hasAttribute || !input.hasAttribute('data-attach-input')) return;
    var chosen = input.files || [];
    for (var i = 0; i < chosen.length; i++) pendingFiles.push(chosen[i]);
    if (input.id !== ATTACH_INPUT_ID) input.value = '';
    syncAttachmentInput();
    renderAttachTiles();
  });

  // Contract §5: a paste that would take the box past its limit becomes a
  // chip instead, and the box is left exactly as it was — never trimmed,
  // never filled with the first N characters. An image is pasted the same
  // way and becomes the same kind of chip, always, whatever it would have
  // done to the box.
  document.addEventListener('paste', function (e) {
    var input = document.getElementById('chat-input');
    if (!input || document.activeElement !== input) return;
    var data = e.clipboardData;
    if (!data) return;
    for (var i = 0; i < data.items.length; i++) {
      var item = data.items[i];
      if (item.kind === 'file' && item.type.indexOf('image/') === 0) {
        var picture = item.getAsFile();
        if (picture) {
          e.preventDefault();
          addPendingFile(picture);
        }
        return;
      }
    }
    var text = data.getData('text/plain');
    if (!text) return;
    var limit = input.maxLength > 0 ? input.maxLength : -1;
    // An approximation — it does not account for a selection the paste would
    // replace — and the honest direction to be wrong in: this is a UI
    // decision about when to lift text out into a chip, not the refusal
    // `chat_exchange.MAX_MESSAGE_CHARS` still enforces on whatever actually
    // arrives (contract §5's own note that the old refusal stays for what it
    // still describes).
    if (limit > -1 && input.value.length + text.length > limit) {
      e.preventDefault();
      addPendingFile(new File([text], 'pasted-text.txt', { type: 'text/plain' }));
    }
  });

  // ── the composer: grows with the message, Enter sends, Shift+Enter breaks ──
  function grow(box) {
    box.style.height = 'auto';
    box.style.height = Math.min(box.scrollHeight, window.innerHeight * 0.4) + 'px';
  }
  document.addEventListener('input', function (e) {
    if (e.target && e.target.id === 'chat-input') { grow(e.target); composerButton(); }
  });
  document.addEventListener('keydown', function (e) {
    if (!e.target || e.target.id !== 'chat-input') return;
    if (e.key !== 'Enter' || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.isComposing) return; // mid-IME, Enter is choosing a character
    e.preventDefault();
    // 2026-09-02: hitting enter is the same thing as clicking the button. So
    // Enter is not "send" — it is whatever the one button is offering right
    // now, which with a turn running and an empty box is stop.
    var button = document.getElementById('chat-send');
    if (button && button.hasAttribute('data-stopping')) {
      stopTheReply();
      return;
    }
    var form = document.getElementById('chat-form');
    if (form && form.requestSubmit) form.requestSubmit();
    else if (form) form.submit();
  });

  // The one button, clicked. In its send state this does nothing at all and
  // the ordinary submit happens — which is what keeps it working in a browser
  // that never ran this file.
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var button = e.target.closest('[data-chat-send]');
    if (!button || !button.hasAttribute('data-stopping')) return;
    e.preventDefault();
    stopTheReply();
  });

  // Taking back a message that is waiting (contract §3). The words go back in
  // the box rather than anywhere else: this undoes a send, it does not delete
  // a message.
  document.addEventListener('click', function (e) {
    if (!e.target.closest || !e.target.closest('[data-chat-unqueue]')) return;
    e.preventDefault();
    takeBackQueued();
  });

  // ── submitting a form that has no button to press ──
  //
  // THE PERSONA PICKER IS NOT ONE OF THESE ANY MORE. It was a <select> this
  // file submitted on change; composer.md §5 made it a list of rows, and each
  // row is the form's own submit — so choosing a persona needs no script at
  // all now and nothing below is involved in it. What is left is the roster's
  // "Add someone…", which is still a select.
  //
  // `requestSubmit`, never `submit()`: only the first fires a submit event, and
  // the submit event is what hx-boost listens for. `submit()` would navigate
  // straight to /admin/chat/persona and leave that in the address bar, which is
  // the "Method Not Allowed on refresh" bug produced by pressing F5.
  //
  // The address bar is no longer this file's problem to get right — the server
  // holds it (`KeepsTheAddressBar`, web/shared.py), because the markup
  // guard that was supposed to do it, `hx-push-url="false"`, turned out to do
  // nothing on a boosted form and the same 405 recurred a second time on
  // v0.13.1. Keeping `requestSubmit` anyway: a boosted submit swaps the page
  // instead of reloading it, which is the behaviour this screen is built on.
  //
  // A browser too old for `requestSubmit` (Safari before 16) gets the clicked
  // button instead, which fires the same event. The <noscript> button in the
  // markup is for a browser with no script at all and is not this case: it is
  // not in the document while this file is running.
  function submitForm(form) {
    if (form.requestSubmit) {
      form.requestSubmit();
      return;
    }
    var press = document.createElement('button');
    press.type = 'submit';
    press.style.display = 'none';
    form.appendChild(press);
    press.click();
    form.removeChild(press);
  }

  document.addEventListener('change', function (e) {
    var select = e.target;
    if (!select || select.id !== 'chat-roster-add') return;
    // Choosing a name is the whole action. The blank first option is the
    // resting state and must not submit — otherwise clicking the control and
    // changing your mind posts an empty persona.
    if (select.value && select.form) submitForm(select.form);
  });

  // ── the stop button ──
  //
  // §5: it ends the exchange at the current turn's boundary. The reply being
  // written finishes and is recorded — the server decides that, and it is
  // right, because a sentence cut in half is a sentence nobody said — and
  // nobody else is asked afterwards.
  //
  // THE AUDIO IS CUT, here, immediately. That is the half §5 leaves to the
  // implementer, and it is what a stop button is expected to do: pressing stop
  // and then listening to another twenty seconds of speech is not stopping.
  // The words already on the screen stay: they were said.
  //
  // The stream is NOT aborted. Aborting would drop the reply in progress before
  // the server had written it down, which is the one thing §5 says not to do.
  document.addEventListener('click', function (e) {
    if (!e.target.closest || !e.target.closest('[data-chat-stop]')) return;
    if (!stopToken) return;
    stopPlaying();
    stopButton(false);
    var body = new FormData();
    body.append('token', stopToken);
    stopToken = '';
    if (window.fetch) {
      fetch('/admin/chat/stop', {
        method: 'POST', body: body, credentials: 'same-origin'
      }).catch(function () {});
    }
  });

  // ── 5. the microphone ──
  //
  // The browser's own recogniser. It streams the audio to the vendor — Google,
  // for every browser that ships this API — which is why the disclosure beside
  // it is revealed at the same moment the button is, and why a badge stays up
  // for as long as the microphone is live. This recogniser was chosen
  // knowingly; the page's job is to be honest about it, not to hide it.
  //
  // Everything below is optional. No recogniser, no microphone, no permission,
  // permission refused: the control never appears or turns itself off with a
  // sentence, and typing is untouched in every one of those cases.
  var Recogniser = window.SpeechRecognition || window.webkitSpeechRecognition;
  var listener = null;
  var committed = '';

  function show(selector, on) {
    var found = document.querySelectorAll(selector);
    for (var i = 0; i < found.length; i++) found[i].hidden = !on;
  }
  function problem(text) {
    var line = document.querySelector('[data-mic-problem]');
    if (!line) return;
    line.textContent = text || '';
    line.hidden = !text;
  }
  function pressed(on) {
    var button = document.querySelector('[data-mic-toggle]');
    if (!button) return;
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    var label = button.querySelector('[data-mic-label]');
    if (label) label.textContent = on ? 'Stop' : 'Dictate';
  }
  function live(on) {
    show('[data-mic-live]', on);
    pressed(on);
  }

  if (Recogniser) show('[data-mic]', true);

  function stop() {
    if (!listener) return;
    var going = listener;
    listener = null;
    try { going.stop(); } catch (err) { /* already stopped */ }
    live(false);
  }

  function start() {
    var input = document.getElementById('chat-input');
    if (!input) return;
    var going;
    try { going = new Recogniser(); } catch (err) {
      problem('This browser would not start its speech recogniser.');
      return;
    }
    going.continuous = true;
    going.interimResults = true; // words as they are said, not after the pause
    going.lang = document.documentElement.lang || 'en-US';

    committed = input.value ? input.value.replace(/\s*$/, '') + ' ' : '';
    problem('');

    going.onresult = function (event) {
      var interim = '';
      for (var i = event.resultIndex; i < event.results.length; i++) {
        var said = event.results[i][0].transcript;
        if (event.results[i].isFinal) committed += said.replace(/^\s+/, '') + ' ';
        else interim += said;
      }
      input.value = committed + interim;
      grow(input);
    };
    going.onerror = function (event) {
      var why = {
        'not-allowed': 'The microphone was not allowed.',
        'service-not-allowed': 'This browser would not use its speech service.',
        'audio-capture': 'No microphone was found.',
        'network': 'The speech recogniser could not be reached.',
        'no-speech': 'Nothing was heard, so dictation stopped.'
      }[event.error];
      problem(why || 'Dictation stopped.');
      stop();
    };
    going.onend = function () { if (listener === going) stop(); };

    listener = going;
    try { going.start(); } catch (err) {
      listener = null;
      problem('Dictation was already running. Press it again.');
      return;
    }
    live(true);
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest || !e.target.closest('[data-mic-toggle]')) return;
    if (listener) stop(); else start();
  });
  // Sending, or leaving the page, is the end of dictation. A microphone that
  // stays live after the screen has moved on is the thing the badge exists to
  // make impossible.
  document.addEventListener('htmx:configRequest', function (e) {
    if (e.detail.elt && e.detail.elt.id === 'chat-form') stop();
  });
  // This one keeps `pagehide` on purpose, unlike the turn above. A microphone
  // left live while the browser is in the background is the exact thing the
  // badge exists to make impossible, and it is worse when the page is merely
  // hidden than when it is gone. `stop()` clears the recogniser and turns the
  // badge off, so a page that comes back from the cache starts dictation again
  // cleanly rather than believing it is already listening.
  window.addEventListener('pagehide', stop);

  // ── 6. the toolbar under a reply ──
  //
  // Copy the reply, and say so. The button is drawn only once this block has
  // run and found somewhere to write — the row is `display: none` in the
  // stylesheet and `can-copy` on <html> is what lifts it. That class goes on
  // the document element rather than the body because hx-boost replaces the
  // body's contents on every navigation and would take it away with them.
  //
  // Two ways to write, because this admin surface is normally reached over
  // plain http on a LAN, and `navigator.clipboard` does not exist outside a
  // secure context. Gating on it alone would have hidden the button on plain
  // http over a LAN. The old `execCommand` path works there, so it
  // is the fallback rather than the absence of one.
  var COPY_NOTE_MS = 2200;

  function writeToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      // Off-screen rather than `display:none`: a box that is not rendered
      // cannot be selected, and an unselected box copies nothing.
      var box = document.createElement('textarea');
      box.value = text;
      box.setAttribute('readonly', '');
      box.style.cssText = 'position:fixed; top:0; left:-9999px; opacity:0';
      // Selecting the box takes the caret out of whatever had it — the message
      // box, mid-sentence, if that is where the operator was. It goes back.
      var had = document.activeElement;
      document.body.appendChild(box);
      box.select();
      var wrote = false;
      try { wrote = document.execCommand('copy'); } catch (err) { wrote = false; }
      document.body.removeChild(box);
      if (had && had.focus) { try { had.focus(); } catch (err) { /* gone */ } }
      if (wrote) resolve(); else reject(new Error('the browser refused to copy'));
    });
  }

  if ((navigator.clipboard && navigator.clipboard.writeText) ||
      typeof document.execCommand === 'function') {
    document.documentElement.classList.add('can-copy');
  }

  // One note at a time, and it goes away again. Both notes are in the markup
  // already — this only chooses which one is showing, so the wording and the
  // icon stay in the template with every other message the surface says.
  function flash(row, selector) {
    var notes = row.querySelectorAll('[data-copy-done], [data-copy-failed]');
    for (var i = 0; i < notes.length; i++) notes[i].hidden = true;
    var note = row.querySelector(selector);
    if (!note) return;
    note.hidden = false;
    window.clearTimeout(row.copyNoteTimer);
    row.copyNoteTimer = window.setTimeout(function () { note.hidden = true; }, COPY_NOTE_MS);
  }

  // Delegated, like everything else here: replies arrive continuously — from a
  // form post, from an htmx swap, from the stream's last frame — and a
  // listener bound to the buttons that existed at load would miss every one of
  // them.
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var button = e.target.closest('[data-copy-reply]');
    if (!button) return;
    e.preventDefault();
    var row = button.parentNode;
    // `getAttribute` gives a string. This is the model's own words and they are
    // never treated as markup — not here and not in the template that wrote
    // the attribute.
    var text = button.getAttribute('data-reply-text') || '';
    writeToClipboard(text).then(
      function () { flash(row, '[data-copy-done]'); },
      function () { flash(row, '[data-copy-failed]'); }
    );
  });

  // ── selecting several conversations at once ──
  // docs/contracts/conversation-list-bulk-actions.md. The mechanism is the
  // checkbox form in chat.html; everything below only drives the same
  // controls it already draws, delegated on `document` for the reason every
  // other listener here is (a reply, or a thread switch, can redraw the rail
  // at any time — see fragments/chat_threads.html's own note on why the
  // checkbox lives where a row is drawn and nowhere else).

  function bulkDeleteForm() {
    return document.querySelector('form[action="/admin/chat/bulk-delete/confirm"]');
  }

  // Delete hides itself until something is picked (contract §6's "a Delete
  // button that appears once something is selected"). The button stays a
  // real, always-present submit control in the markup a script never
  // reached — this only narrows when it is shown, never what it does.
  function bulkSyncDeleteButton() {
    var form = bulkDeleteForm();
    if (!form) return;
    var button = form.querySelector('button[type="submit"]');
    if (!button) return;
    var all = form.querySelector('input[name="select_all"]');
    var any = (all && all.checked) ||
      !!form.querySelector('input[name="selected"]:checked');
    button.hidden = !any;
  }

  // The select-all checkbox is a real control on its own (the server treats
  // it as "everything currently visible" whatever the row checkboxes say —
  // see chat.py's `bulk_selected_rows`), but a person looking at the rail
  // expects ticking it to visibly tick every row, not to work invisibly.
  function bulkToggleAll(checked) {
    var form = bulkDeleteForm();
    if (!form) return;
    var rows = form.querySelectorAll('input[name="selected"]');
    for (var i = 0; i < rows.length; i++) rows[i].checked = checked;
  }

  document.addEventListener('change', function (e) {
    if (!e.target.matches) return;
    if (e.target.matches('input[name="select_all"]')) {
      bulkToggleAll(e.target.checked);
    } else if (!e.target.matches('input[name="selected"]')) {
      return;
    } else if (!e.target.checked) {
      // Unticking a row unties Select all. Without this the box stayed
      // ticked while a row beneath it did not, and the screen was then
      // saying two different things at once -- which is how a user could delete
      // conversations they had just unticked. The server no longer believes
      // the flag over the boxes (`bulk_selected_rows`), so this is about
      // what the rail *says*, and a control that lies about what is
      // selected is a control nobody can use for a destructive action.
      var all = bulkDeleteForm() && bulkDeleteForm().querySelector('input[name="select_all"]');
      if (all) all.checked = false;
    }
    bulkSyncDeleteButton();
  });

  // The rail redraws with every fresh checkbox unticked — a reply landing, a
  // thread switch — whether or not anybody had ticked one before. Delete's
  // visibility has to catch up with it rather than remembering a selection
  // that the swap already cleared.
  document.addEventListener('htmx:afterSwap', function () { bulkSyncDeleteButton(); });

  // Long press: the gesture a touch screen has instead of a pointer hovering
  // a checkbox (contract §3's touch column). 500ms without the finger moving
  // more than a few pixels toggles that row's own checkbox — the same one
  // the desktop form already draws — and swallows the `click` a touchend
  // would otherwise fire, so a long press never also navigates to the
  // conversation it just selected.
  var LONG_PRESS_MS = 500;
  var LONG_PRESS_SLOP = 10; // px of finger movement that cancels a press
  var pressTimer = null;
  var pressStart = null;
  var longPressedRow = null;

  function bulkRowCheckbox(thread) {
    var row = thread.closest('.hrow');
    return row ? row.querySelector('input[name="selected"]') : null;
  }

  function bulkClearPress() {
    window.clearTimeout(pressTimer);
    pressTimer = null;
    pressStart = null;
  }

  document.addEventListener('touchstart', function (e) {
    var thread = e.target.closest && e.target.closest('.chat-thread');
    var box = thread && bulkRowCheckbox(thread);
    if (!box || !e.touches || e.touches.length !== 1) return;
    pressStart = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    pressTimer = window.setTimeout(function () {
      box.checked = !box.checked;
      bulkSyncDeleteButton();
      longPressedRow = thread;
      pressTimer = null;
    }, LONG_PRESS_MS);
  }, { passive: true });

  document.addEventListener('touchmove', function (e) {
    if (!pressStart || !e.touches || !e.touches.length) return;
    var dx = e.touches[0].clientX - pressStart.x;
    var dy = e.touches[0].clientY - pressStart.y;
    if (Math.sqrt(dx * dx + dy * dy) > LONG_PRESS_SLOP) bulkClearPress();
  }, { passive: true });

  document.addEventListener('touchend', bulkClearPress);
  document.addEventListener('touchcancel', bulkClearPress);

  // The synthesised click that follows `touchend`. Only swallowed on the
  // exact row a long press just fired for, and only once — an ordinary tap
  // elsewhere, including on the checkbox itself, is untouched.
  document.addEventListener('click', function (e) {
    if (!longPressedRow) return;
    var thread = e.target.closest && e.target.closest('.chat-thread');
    if (thread === longPressedRow) {
      e.preventDefault();
      e.stopPropagation();
    }
    longPressedRow = null;
  }, true);

  // ── the first look at the page, after everything above exists ──
  //
  // Down here rather than beside `settle()` at the top, and the reason is the
  // one thing that makes a file like this fail silently: `var` declarations
  // are hoisted but their assignments are not. `streamable`, `inFlight` and
  // the attach budget are all declared halfway down, so anything run from the
  // top of the file reads them as `undefined` — and `attachIfTurnRunning`
  // would have declined every single time, with no error and nothing to see.
  //
  // This is what makes §6 true on a page that was merely opened: the tablet
  // that slept through the middle of a turn comes back to a fresh page load,
  // and the server has already said on the form whether a reply is still being
  // written in this thread. Nothing to press.
  function begin() {
    composerButton();
    renderQueued();
    attachIfTurnRunning();
    bulkSyncDeleteButton();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', begin);
  else begin();
})();
