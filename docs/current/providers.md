# Answer providers

The answer-generation interface currently supports Ollama and Gemini. Embedding generation uses a separate local provider and configuration. `lens model show` reports the effective answer provider/model; `lens model use` persists a selection, and command-line options can override it for a request.

Ollama sends prompts to the configured Ollama host. Its model weights are managed by the user outside Lens. Gemini support is optional and requires the Google GenAI extra plus `GEMINI_API_KEY`; it is an explicit remote choice that sends selected evidence to Google. Lens does not silently fall back from one provider to another. Provider transport errors, unsupported model responses, and validation errors are reported rather than represented as successful answers.

Use `lens model test` to check provider availability; `--generate` sends a minimal generation request. The latter may incur remote usage when Gemini is selected. Keep keys in local configuration or environment variables and never copy them into a trace, report, or issue.
