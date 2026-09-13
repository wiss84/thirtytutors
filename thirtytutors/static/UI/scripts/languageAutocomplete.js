/**
 * Reusable language-name autocomplete for the native/target language text
 * inputs on /get-started, /avatar-select, and the Settings modal's
 * General tab (see thirtytutors/languages.py's own module docstring for
 * why this is a suggestion aid, not a hard allowlist - every input stays
 * plain free text underneath, on or off this list).
 *
 * Self-contained: attachLanguageAutocomplete(inputEl) wraps the given
 * input in its own positioning wrapper and builds the dropdown itself, so
 * a page only needs to call it - no HTML markup changes needed on the
 * consuming page. Loaded globally (see index.html) so every page that
 * needs it already has it, the same way theme.js/statsPane.js are.
 */

let languagesPromise = null;

function loadLanguages() {
  // Fetched once and shared across every input on the page, and across
  // every page for the rest of this session - the list is static for the
  // lifetime of the running app, no reason to refetch it per input or per
  // keystroke.
  if (!languagesPromise) {
    languagesPromise = fetch('/api/languages')
      .then((r) => r.json())
      .then((data) => data.languages || [])
      .catch(() => []); // fails open - a missing suggestion list should never block typing a language name manually
  }
  return languagesPromise;
}

function attachLanguageAutocomplete(inputEl) {
  if (!inputEl || inputEl.dataset.langAutocompleteAttached) return;
  inputEl.dataset.langAutocompleteAttached = 'true';
  inputEl.setAttribute('autocomplete', 'off'); // avoid the browser's own native suggestion dropdown fighting with this one

  const wrap = document.createElement('div');
  wrap.className = 'lang-autocomplete-wrap';
  inputEl.parentNode.insertBefore(wrap, inputEl);
  wrap.appendChild(inputEl);

  const dropdown = document.createElement('div');
  dropdown.className = 'lang-autocomplete-dropdown';
  wrap.appendChild(dropdown);

  let allLanguages = [];
  loadLanguages().then((list) => {
    allLanguages = list;
  });

  let matches = [];
  let activeIndex = -1;

  function close() {
    dropdown.classList.remove('visible');
    dropdown.innerHTML = '';
    matches = [];
    activeIndex = -1;
  }

  function render() {
    dropdown.innerHTML = '';
    matches.forEach((lang, i) => {
      const item = document.createElement('div');
      item.className = 'lang-autocomplete-item' + (i === activeIndex ? ' active' : '');
      item.textContent = lang;
      // mousedown (not click) - fires before the input's own blur, so
      // picking a suggestion by clicking isn't raced/cancelled by the
      // blur handler below closing the dropdown first.
      item.addEventListener('mousedown', (e) => {
        e.preventDefault();
        inputEl.value = lang;
        close();
        // So any existing input-driven validation already on the page
        // (e.g. get_started's Next-button gating on nativeLanguageInput)
        // still fires the same way it would for a person typing.
        inputEl.dispatchEvent(new Event('input', { bubbles: true }));
      });
      dropdown.appendChild(item);
    });
    dropdown.classList.toggle('visible', matches.length > 0);
  }

  function updateMatches() {
    const query = inputEl.value.trim().toLowerCase();
    if (!query) {
      matches = [];
      activeIndex = -1;
      render();
      return;
    }
    // "Starts with" matches first (most relevant to what's being typed),
    // then "contains" matches, each group alphabetical (allLanguages is
    // already sorted - see languages.py) - capped so the list stays
    // scannable rather than dumping the whole ~95-entry list for a
    // single-letter query.
    const startsWith = [];
    const contains = [];
    for (const lang of allLanguages) {
      const lower = lang.toLowerCase();
      if (lower.startsWith(query)) startsWith.push(lang);
      else if (lower.includes(query)) contains.push(lang);
    }
    matches = [...startsWith, ...contains].slice(0, 8);
    activeIndex = -1;
    render();
  }

  inputEl.addEventListener('input', updateMatches);
  inputEl.addEventListener('focus', updateMatches);
  inputEl.addEventListener('blur', () => close());

  inputEl.addEventListener('keydown', (e) => {
    if (!matches.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      activeIndex = Math.min(activeIndex + 1, matches.length - 1);
      render();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      render();
    } else if (e.key === 'Enter') {
      if (activeIndex >= 0) {
        e.preventDefault();
        inputEl.value = matches[activeIndex];
        close();
        inputEl.dispatchEvent(new Event('input', { bubbles: true }));
      }
    } else if (e.key === 'Escape') {
      close();
    }
  });
}

window.attachLanguageAutocomplete = attachLanguageAutocomplete;
