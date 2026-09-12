# Automatic `.env` Loading Design

## Goal

Load repository-local configuration from `.env` automatically whenever the CLI starts, so operators do not need to source the file manually.

## Behavior

The CLI calls `python-dotenv` during application construction. It searches from the current working directory using the library's standard lookup, loads `.env` when present, and leaves any variable already defined in the process environment unchanged. A missing `.env` is a normal no-op.

`.env` and `.env.*` remain ignored by Git, except for the existing safe `.env.example` template. Secrets are never printed or copied into run artifacts.

## Scope

This changes CLI startup only. Library consumers that construct `Pipeline` directly retain explicit environment handling. No provider, model, or credential validation changes are included.

## Validation

A CLI-level test changes into a temporary directory containing `.env`, constructs the application, and confirms the configured search endpoint reaches pipeline construction. A second assertion establishes that a shell-provided value takes precedence over `.env`.
