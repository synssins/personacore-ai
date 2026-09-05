"""Workspace settings — ``[workspace]`` in ``core.toml``.

``working/contracts/workspace.md`` sections 4 and 9 name four knobs, shown on
the Core settings screen with defaults filled in rather than left blank: how
much of one tool result (or one workspace file read) the model receives
before it is cut, how long a plain tool result has to be before it is saved
to the conversation's workspace as a file instead of being cut, and how big
one file or one conversation's whole workspace folder may grow.

The workspace root itself is not a setting — it is fixed at
``<appdata>/workspaces`` (contract §9) the same way every other appdata
folder is, so it has no field here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkspaceSettings(BaseModel):
    """``[workspace]`` — the shape every implementer builds to (contract §9)."""

    model_config = ConfigDict(extra="forbid")

    tool_result_chars: int = Field(default=64000, ge=256)
    """The most characters of one tool result the model receives in a single
    turn. Past this the text is cut and the cut is marked, so the model is
    never quietly handed less than it thinks it has (contract §4)."""

    long_item_chars: int = Field(default=8000, ge=256)
    """With a workspace turned on, a plain-text tool result longer than this
    is saved to the conversation's workspace as a file instead of being
    handed to the model whole (contract §4). Used from step 3 of the
    contract on; declared now so the setting exists before the behaviour
    that reads it does."""

    max_file_bytes: int = Field(default=2_000_000, ge=1024)
    """The largest a single workspace file may grow. A write past this limit
    is refused with a plain message naming the limit (contract §5)."""

    max_workspace_bytes: int = Field(default=50_000_000, ge=1024)
    """The largest one conversation's whole workspace folder may grow, all
    its files together. A write that would push the folder past this limit
    is refused the same way (contract §5)."""

    @model_validator(mode="after")
    def _long_item_chars_within_tool_result_chars(self) -> WorkspaceSettings:
        """The file threshold cannot sit past the amount the model can see.

        A `long_item_chars` greater than `tool_result_chars` would name a
        length nothing could ever reach uncut — the §4 cap would always cut
        the result first, so the file threshold would never fire. Caught here
        rather than left as a hard-to-notice interaction between two numbers
        on separate rows of the settings screen.
        """
        if self.long_item_chars > self.tool_result_chars:
            raise ValueError(
                "'long_item_chars' must be no greater than 'tool_result_chars' — "
                "a result cannot be saved as a file for being longer than a cap "
                "it would never reach uncut."
            )
        return self


__all__ = ["WorkspaceSettings"]
