/* admin.js — everything beyond htmx, small enough to read in one sitting.
   Three jobs:
   1. Modal plumbing: fragments swap into <dialog id="modal">; open on swap,
      close on [data-modal-close] / after a confirmed action / Esc / backdrop.
   2. String-list & repeating-group edits that must NOT round-trip, because a
      server re-render would wipe unsaved values in sibling fields:
      remove, move up, move down, add-string-row.
   3. data-copy / data-copy-from buttons, and the file-input label.
   Plus: closing every other open search result list when one opens, so the
   "only one search at a time" rule holds on the page as well as per request. */
(function () {
  'use strict';

  // ── 1. modal ──
  var modal = null;
  function getModal() { return modal || (modal = document.getElementById('modal')); }

  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (e.target === getModal() && getModal().innerHTML.trim() !== '') {
      getModal().showModal();
    }
  });

  // Only one search open at a time: opening a result list closes every other.
  // The server already runs at most one lookup per request; this is the same
  // rule on the page, where two open lists would invite picking from the stale
  // one. Cleared rather than re-requested — nothing is called to close a list.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (!t || !t.classList || !t.classList.contains('search-results')) return;
    var all = document.querySelectorAll('.search-results');
    for (var i = 0; i < all.length; i++) {
      if (all[i] !== t) all[i].innerHTML = '';
    }
  });
  document.addEventListener('click', function (e) {
    var m = getModal();
    if (!m || !m.open) return;
    if (e.target.closest('[data-modal-close]')) { m.close(); m.innerHTML = ''; }
    if (e.target === m) { m.close(); m.innerHTML = ''; } // backdrop click
  });
  // a confirmed destructive action closes the modal once its request lands
  document.body.addEventListener('htmx:afterRequest', function (e) {
    if (e.detail.elt && e.detail.elt.hasAttribute('data-modal-close-after') && e.detail.successful) {
      var m = getModal(); if (m && m.open) { m.close(); m.innerHTML = ''; }
    }
  });

  // ── 2. list edits that preserve unsaved sibling values ──
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-entry-action]');
    if (btn) {
      var item = btn.closest('.strlist-item, fieldset.entry');
      if (!item) return;
      var action = btn.getAttribute('data-entry-action');
      if (action === 'remove') item.remove();
      if (action === 'up' && item.previousElementSibling &&
          item.previousElementSibling.matches('.strlist-item, fieldset.entry')) {
        item.parentNode.insertBefore(item, item.previousElementSibling);
      }
      if (action === 'down' && item.nextElementSibling &&
          item.nextElementSibling.matches('.strlist-item, fieldset.entry')) {
        item.parentNode.insertBefore(item.nextElementSibling, item);
      }
      return;
    }

    var add = e.target.closest('[data-strlist-add]');
    if (add) {
      var list = add.closest('[data-strlist]');
      var input = list.querySelector('[data-strlist-input]');
      var value = (input.value || '').trim();
      if (!value) return;
      var key = list.getAttribute('data-strlist');
      var row = document.createElement('div');
      row.className = 'strlist-item';
      row.innerHTML =
        '<input type="hidden">' +
        '<span class="grow"></span>' +
        '<button type="button" class="btn btn-icon btn-secondary" data-entry-action="up" aria-label="Move up">\u2191</button>' +
        '<button type="button" class="btn btn-icon btn-secondary" data-entry-action="down" aria-label="Move down">\u2193</button>' +
        '<button type="button" class="btn btn-icon btn-danger" data-entry-action="remove" aria-label="Remove">\u00d7</button>';
      row.querySelector('input').name = key;
      row.querySelector('input').value = value;
      row.querySelector('.grow').textContent = value;
      var empty = list.querySelector('[data-strlist-empty]');
      if (empty) empty.remove();
      list.insertBefore(row, input.closest('.hrow'));
      input.value = '';
      input.focus();
    }
  });
  // Enter in the add-input adds instead of submitting the settings form
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.matches('[data-strlist-input]')) {
      e.preventDefault();
      var list = e.target.closest('[data-strlist]');
      var btn = list && list.querySelector('[data-strlist-add]');
      if (btn) btn.click();
    }
  });

  // ── 3. copy buttons + file-input label ──
  document.addEventListener('click', function (e) {
    var copy = e.target.closest('[data-copy], [data-copy-from]');
    if (!copy) return;
    // data-copy-from names an element to read instead of carrying the value in
    // an attribute. That matters for exactly one thing on this surface: a newly
    // issued key is rendered once, and a copy button holding a second copy of it
    // would put the credential in the page twice.
    var from = copy.getAttribute('data-copy-from');
    var source = from ? document.querySelector(from) : null;
    var text = from ? ((source && source.textContent) || '') : copy.getAttribute('data-copy');
    var done = function () {
      var was = copy.textContent;
      copy.textContent = 'copied';
      setTimeout(function () { copy.textContent = was; }, 2000);
    };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, done);
  });
  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-file-label]')) {
      var label = document.querySelector('[data-file-name]');
      if (label) label.textContent = e.target.files.length ? e.target.files[0].name : 'no file chosen';
    }
  });

  // Play a test line once, when it was asked for -- and never again.
  //
  // The audio element carried `autoplay`, which fires whenever the browser
  // renders it. Navigate away from a voice and come back, and the page is
  // restored from cache with the element intact, so the last thing tested plays
  // itself unbidden. This was found immediately.
  //
  // A swap is the press: htmx only swaps because somebody clicked. A restore is
  // not, and produces no swap, so nothing plays. Without JavaScript the element
  // has ordinary controls and a play button, which is the correct fallback --
  // the alternative was audio that cannot be stopped and starts by surprise.
  //
  // The LAST match, and the attribute is taken off once it has been used. Both
  // halves are the chat screen: a reply is appended into a list that already
  // holds the earlier ones, so `querySelector` found the FIRST player in the
  // conversation and every new reply played the first one again. Taking the
  // newest fixes that; removing the attribute is what makes the name true, and
  // stops a later swap into the same region finding a player that has already
  // had its turn.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var all = e.target && e.target.querySelectorAll
      ? e.target.querySelectorAll('audio[data-play-once]') : null;
    var audio = all && all.length ? all[all.length - 1] : null;
    if (audio && audio.play) {
      audio.removeAttribute('data-play-once');
      var p = audio.play();
      if (p && p.catch) p.catch(function () {});
    }
  });

  // Show the refusal the server already wrote.
  //
  // Every form here is an htmx request -- `hx-boost` on the body catches even
  // the plain ones -- and htmx does not swap a 4xx by default. But this
  // application answers a rejected form by rendering the same screen again with
  // a plain-English message on it, at 400, 422 or 403. That page was being
  // built, sent, and thrown away: pressing Add account with a short password
  // did nothing at all, no message, no change. That was found in one click.
  //
  // Only 4xx, and only when the response carries a page to show. A 5xx is not
  // a message to the operator -- swapping a stack trace into the screen would
  // replace "nothing happened" with something worse -- and neither is an empty
  // body, which would blank the screen it was meant to correct.
  document.body.addEventListener('htmx:beforeSwap', function (e) {
    var status = e.detail && e.detail.xhr && e.detail.xhr.status;
    if (status >= 400 && status < 500 && e.detail.xhr.responseText) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });
})();
