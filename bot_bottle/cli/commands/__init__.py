"""CLI subcommand handlers — one module per `bot-bottle <command>`.

Each module exposes a `cmd_<name>(argv)` handler that the dispatcher
(`bot_bottle.cli`) registers in its COMMANDS table. Shared CLI helpers
(`_common`, `tui`) stay one level up in the `cli` package.
"""
