"""Curated list of language display names shown as autocomplete
suggestions for the native/target language text inputs on /get-started,
/avatar-select, and the Settings modal's General tab (see /api/languages
in routes_api.py, and languageAutocomplete.js on the frontend for how
it's actually used).

Deliberately NOT a hard allowlist or validation gate - every one of those
inputs stays plain free text underneath; a person can still type anything
at all, on or off this list. Two reasons for that, not just one:

1. Gemini's native audio models (see live_session.py's build_config) -
   which is what this app always uses - don't take an explicit language
   code config at all. They infer the spoken language from context,
   chiefly the plain language name this app injects into
   tutor_instructions.py's system instruction. There's no API-level
   "supported languages" list to validate against in the first place for
   how this app actually talks to the model.
2. Google's own docs disagree with each other on a supported-language
   COUNT across different product surfaces (24 vs 70, depending which
   page), and the one place a precise, authoritative per-language table
   does exist is for a config field (SpeechConfig.language_code) that's
   specific to the Vertex/Enterprise platform this app doesn't use (see
   design_plans/issues.md #1's own note on transparent=True being
   Enterprise-only for the exact same reason). Given that, a hard
   allowlist built from either source would risk excluding something the
   model actually handles fine - worse than today's free-text input, not
   better.

So this list exists purely to help someone type a well-formed, correctly
spelled name faster and with fewer typos.

Regional/dialect variants are included only where they're genuinely
distinct spoken varieties commonly relevant to a language learner (e.g.
Arabic's dialects differ enough conversationally that "Egyptian Arabic"
vs "Levantine Arabic" is a meaningful choice, not a cosmetic one) - not
exhaustively for every language, so this stays a genuinely scannable list
rather than an unmanageable one.
"""

LANGUAGES = sorted(
    [
        "Afrikaans",
        "Albanian",
        "Amharic",
        "Arabic (Modern Standard)",
        "Arabic (Egyptian)",
        "Arabic (Gulf)",
        "Arabic (Iraqi)",
        "Arabic (Levantine)",
        "Arabic (Moroccan / Darija)",
        "Armenian",
        "Azerbaijani",
        "Basque",
        "Belarusian",
        "Bengali",
        "Bosnian",
        "Bulgarian",
        "Burmese",
        "Catalan",
        "Cebuano",
        "Chinese (Cantonese)",
        "Chinese (Mandarin)",
        "Croatian",
        "Czech",
        "Danish",
        "Dutch",
        "English (Australia)",
        "English (Canada)",
        "English (India)",
        "English (UK)",
        "English (US)",
        "Estonian",
        "Filipino / Tagalog",
        "Finnish",
        "French (Canada)",
        "French (France)",
        "Galician",
        "Georgian",
        "German (Austria)",
        "German (Germany)",
        "German (Switzerland)",
        "Greek",
        "Gujarati",
        "Haitian Creole",
        "Hausa",
        "Hebrew",
        "Hindi",
        "Hungarian",
        "Icelandic",
        "Igbo",
        "Indonesian",
        "Italian",
        "Japanese",
        "Javanese",
        "Kannada",
        "Kazakh",
        "Khmer",
        "Korean",
        "Kurdish",
        "Lao",
        "Latvian",
        "Lithuanian",
        "Macedonian",
        "Malay",
        "Malayalam",
        "Maltese",
        "Marathi",
        "Mongolian",
        "Nepali",
        "Norwegian",
        "Pashto",
        "Persian / Farsi",
        "Polish",
        "Portuguese (Brazil)",
        "Portuguese (Portugal)",
        "Punjabi",
        "Romanian",
        "Russian",
        "Serbian",
        "Sinhala",
        "Slovak",
        "Slovenian",
        "Somali",
        "Spanish (Argentina)",
        "Spanish (Latin America)",
        "Spanish (Mexico)",
        "Spanish (Spain)",
        "Spanish (USA)",
        "Swahili",
        "Swedish",
        "Tamil",
        "Telugu",
        "Thai",
        "Turkish",
        "Ukrainian",
        "Urdu",
        "Uzbek",
        "Vietnamese",
        "Welsh",
        "Xhosa",
        "Yoruba",
        "Zulu",
    ]
)
