#!/usr/bin/env python3
"""Minimal stdio MCP server for explicit multi-workspace Notion access.

This server implements:

- list_workspaces
- search
- fetch_page
- fetch_database
- query_database
- create_page
- append_block_children
- upload_file
- upload_and_append_file_block

Every Notion tool call requires an explicit workspace selector. Workspace
configuration is normalized around a workspace key list so the server can safely
support more than two workspaces without changing code.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SERVER_NAME = "notion-multi-workspace"
SERVER_VERSION = "0.4.0"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
FILE_UPLOAD_NOTION_VERSION = "2026-03-11"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DOTENV_ENV_VAR = "NOTION_MULTI_WORKSPACE_ENV_FILE"
DOTENV_PATH = PLUGIN_ROOT / ".env"
WORKSPACE_KEYS_ENV_VAR = "NOTION_WORKSPACE_KEYS"
UPLOAD_ROOTS_ENV_VAR = "NOTION_UPLOAD_ROOTS"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024
MAX_NOTION_RESPONSE_BYTES = 1024 * 1024
NOTION_REQUEST_TIMEOUT_SECONDS = 30
MAX_PUBLIC_ERROR_BYTES = 1024
MAX_SAFE_STDERR_BYTES = 512
MAX_MCP_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_MCP_HEADER_LINE_BYTES = 8192
MAX_MCP_HEADERS = 32
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)
EXACT_UUID_RE = re.compile(
    r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


class ConfigError(RuntimeError):
    """Raised when the plugin configuration is incomplete."""


class NotionApiError(RuntimeError):
    """Raised when the Notion API responds with an error."""

    def __init__(
        self,
        message: str = "Notion API request failed.",
        *,
        status: int | None = None,
        api_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None
        self.api_code = (
            api_code
            if isinstance(api_code, str)
            and re.fullmatch(r"[a-z0-9_]{1,64}", api_code)
            else None
        )


class McpProtocolError(RuntimeError):
    """Raised when a request is invalid."""


def safe_error_record(exc: BaseException) -> dict[str, Any]:
    """Return a bounded error record without converting the exception to text."""

    internal_record = {
        "category": "internal",
        "message": "Internal server error.",
    }
    try:
        if isinstance(exc, ConfigError):
            return {
                "category": "configuration",
                "message": "Configuration is incomplete or invalid.",
            }
        if isinstance(exc, McpProtocolError):
            return {
                "category": "invalid_request",
                "message": "Request or tool arguments are invalid.",
            }
        if isinstance(exc, NotionApiError):
            record: dict[str, Any] = {
                "category": "notion_api",
                "message": "Notion API request failed.",
            }
            try:
                status = exc.status
                api_code = exc.api_code
            except BaseException:
                return record
            if (
                isinstance(status, int)
                and not isinstance(status, bool)
                and 100 <= status <= 599
            ):
                record["status"] = status
            if isinstance(api_code, str) and re.fullmatch(
                r"[a-z0-9_]{1,64}", api_code
            ):
                record["code"] = api_code
            return record
        return internal_record
    except BaseException:
        return internal_record


def safe_tool_error(exc: BaseException) -> dict[str, Any]:
    """Build a redacted MCP tool error result."""

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"error": safe_error_record(exc)},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
        ],
        "isError": True,
    }


def write_safe_stderr(event: str, exc: BaseException) -> None:
    """Emit one bounded diagnostic record without traceback or exception text."""

    safe_event = (
        event
        if isinstance(event, str) and re.fullmatch(r"[a-z0-9_]{1,64}", event)
        else "internal_error"
    )
    try:
        encoded = json.dumps(
            {"event": safe_event, "error": safe_error_record(exc)},
            separators=(",", ":"),
            sort_keys=True,
        )
        sys.stderr.write(encoded[: MAX_SAFE_STDERR_BYTES - 1] + "\n")
    except Exception:
        return


@dataclass(frozen=True)
class WorkspaceConfig:
    """Configured Notion workspace binding."""

    key: str
    name: str
    token: str
    extra_aliases: tuple[str, ...] = ()

    @property
    def aliases(self) -> tuple[str, ...]:
        candidates = {self.key, self.name.strip().lower(), slugify(self.name)}
        candidates.update(alias.strip().lower() for alias in self.extra_aliases if alias.strip())
        candidates.update(slugify(alias) for alias in self.extra_aliases if alias.strip())
        return tuple(sorted(alias for alias in candidates if alias))


EXPECTED_ENV_VARS = (WORKSPACE_KEYS_ENV_VAR,)


def _open_absolute_directory_nofollow(path: Path) -> int:
    """Open an absolute directory one component at a time without following links."""

    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ConfigError("Upload roots must be normalized absolute directories.")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    failed_close_descriptor = -1
    try:
        descriptor = os.open("/", directory_flags)
        for component in path.parts[1:]:
            next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            previous_descriptor = descriptor
            descriptor = next_descriptor
            try:
                os.close(previous_descriptor)
            except OSError:
                failed_close_descriptor = previous_descriptor
                raise
        result = descriptor
        descriptor = -1
        return result
    except OSError:
        raise ConfigError(
            "Every upload root component must be an existing real directory."
        ) from None
    finally:
        for open_descriptor in (descriptor, failed_close_descriptor):
            if open_descriptor >= 0:
                try:
                    os.close(open_descriptor)
                except OSError:
                    pass


def parse_upload_roots(raw_value: str | None) -> tuple[Path, ...]:
    """Parse the configured local upload roots."""

    if raw_value is None or not raw_value.strip():
        raise McpProtocolError(
            "Local file uploads are disabled until NOTION_UPLOAD_ROOTS is configured."
        )

    raw_roots = raw_value.split(os.pathsep)
    if any(not item.strip() for item in raw_roots):
        raise ConfigError("NOTION_UPLOAD_ROOTS contains an empty entry.")

    roots: list[Path] = []
    seen: set[Path] = set()
    for raw_root in raw_roots:
        if raw_root != raw_root.strip() or "\x00" in raw_root:
            raise ConfigError("Upload roots must be normalized absolute directories.")
        root = Path(raw_root)
        if root == Path(root.anchor):
            raise ConfigError("The filesystem root cannot be used as an upload root.")
        descriptor = _open_absolute_directory_nofollow(root)
        os.close(descriptor)
        if root in seen:
            raise ConfigError("NOTION_UPLOAD_ROOTS contains a duplicate entry.")
        seen.add(root)
        roots.append(root)
    return tuple(roots)


def _open_leaf_beneath_root(root: Path, relative_parts: tuple[str, ...]) -> int:
    """Open a leaf beneath a trusted root without following any link component."""

    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        raise McpProtocolError("Upload file path is invalid.")

    descriptor = -1
    failed_close_descriptor = -1
    try:
        try:
            descriptor = _open_absolute_directory_nofollow(root)
            directory_flags = (
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            for component in relative_parts[:-1]:
                next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
                previous_descriptor = descriptor
                descriptor = next_descriptor
                try:
                    os.close(previous_descriptor)
                except OSError:
                    failed_close_descriptor = previous_descriptor
                    raise
            path_metadata = os.stat(
                relative_parts[-1], dir_fd=descriptor, follow_symlinks=False
            )
            if not stat.S_ISREG(path_metadata.st_mode):
                raise McpProtocolError("Upload file must be a regular file.")
            leaf_descriptor = -1
            try:
                leaf_descriptor = os.open(
                    relative_parts[-1],
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=descriptor,
                )
                if _upload_identity(os.fstat(leaf_descriptor)) != _upload_identity(
                    path_metadata
                ):
                    raise McpProtocolError("Upload file changed while it was opened.")
                return leaf_descriptor
            except Exception:
                if leaf_descriptor >= 0:
                    os.close(leaf_descriptor)
                raise
        except (ConfigError, OSError):
            raise McpProtocolError(
                "Upload file must be a regular file beneath an approved root with no links."
            ) from None
    finally:
        for open_descriptor in (descriptor, failed_close_descriptor):
            if open_descriptor >= 0:
                try:
                    os.close(open_descriptor)
                except OSError:
                    pass


def _upload_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the stable metadata used to detect replacement or mutation races."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


class SecureUploadFile:
    """A held, prevalidated local file descriptor for one upload request."""

    def __init__(
        self,
        descriptor: int,
        root: Path,
        relative_parts: tuple[str, ...],
        metadata: os.stat_result,
    ) -> None:
        self._descriptor = descriptor
        self._root = root
        self._relative_parts = relative_parts
        self._identity = _upload_identity(metadata)
        self.size = metadata.st_size
        self.local_name = relative_parts[-1]

    def __enter__(self) -> SecureUploadFile:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def fileno(self) -> int:
        return self._descriptor

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def revalidate(self) -> None:
        """Confirm the held descriptor and current path still name the same file."""

        if self._descriptor < 0:
            raise McpProtocolError("Upload file is no longer open.")
        held_metadata = os.fstat(self._descriptor)
        if not stat.S_ISREG(held_metadata.st_mode):
            raise McpProtocolError("Upload file must remain a regular file.")
        if _upload_identity(held_metadata) != self._identity:
            raise McpProtocolError("Upload file changed before it could be sent.")

        current_descriptor = _open_leaf_beneath_root(
            self._root, self._relative_parts
        )
        try:
            current_metadata = os.fstat(current_descriptor)
            if _upload_identity(current_metadata) != self._identity:
                raise McpProtocolError("Upload file changed before it could be sent.")
        finally:
            os.close(current_descriptor)

    def iter_chunks(self, chunk_size: int = 64 * 1024):
        """Yield bounded chunks from the held descriptor without whole-file buffering."""

        if not isinstance(chunk_size, int) or not 1 <= chunk_size <= 1024 * 1024:
            raise McpProtocolError("Upload chunk size is invalid.")
        self.revalidate()
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        remaining = self.size
        while remaining:
            chunk = os.read(self._descriptor, min(chunk_size, remaining))
            if not chunk:
                raise McpProtocolError("Upload file changed while it was being read.")
            remaining -= len(chunk)
            yield chunk
        if os.read(self._descriptor, 1):
            raise McpProtocolError("Upload file changed while it was being read.")
        if _upload_identity(os.fstat(self._descriptor)) != self._identity:
            raise McpProtocolError("Upload file changed while it was being read.")


def open_approved_upload(
    file_path: str, roots: tuple[Path, ...]
) -> SecureUploadFile:
    """Open a local upload beneath one configured root without following links."""

    if not roots:
        raise McpProtocolError("Local file uploads are disabled.")
    if not isinstance(file_path, str) or not file_path or "\x00" in file_path:
        raise McpProtocolError("Upload requires an absolute file_path.")
    candidate = Path(file_path)
    if not candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts[1:]
    ):
        raise McpProtocolError(
            "Upload file_path must be normalized, absolute, and free of traversal."
        )

    matching_root: Path | None = None
    relative_parts: tuple[str, ...] = ()
    for root in sorted(roots, key=lambda item: len(item.parts), reverse=True):
        if candidate.parts[: len(root.parts)] == root.parts:
            matching_root = root
            relative_parts = candidate.parts[len(root.parts) :]
            break
    if matching_root is None or not relative_parts:
        raise McpProtocolError("Upload file_path is outside the approved roots.")

    descriptor = _open_leaf_beneath_root(matching_root, relative_parts)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise McpProtocolError("Upload file must be a regular file.")
        if metadata.st_size < 0 or metadata.st_size > MAX_UPLOAD_BYTES:
            raise McpProtocolError("Upload file exceeds the 20 MiB limit.")
        return SecureUploadFile(
            descriptor, matching_root, relative_parts, metadata
        )
    except Exception:
        os.close(descriptor)
        raise


def validate_upload_filename(filename: str) -> str:
    """Validate a multipart filename."""

    if not isinstance(filename, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._ -]{0,254}", filename
    ):
        raise McpProtocolError(
            "Upload filenames must use only safe ASCII letters, digits, spaces, dots, dashes, and underscores."
        )
    if filename in {".", ".."} or len(filename.encode("utf-8")) > 255:
        raise McpProtocolError("Upload filename is invalid.")
    return filename


def validate_upload_content_type(content_type: str) -> str:
    """Validate a multipart MIME type."""

    if not isinstance(content_type, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}",
        content_type,
    ):
        raise McpProtocolError("Upload content type must be a simple valid MIME type.")
    return content_type.lower()


class MultipartFileBody:
    """Iterable body for one bounded multipart file part."""

    def __init__(
        self,
        upload: SecureUploadFile,
        filename: str,
        content_type: str,
    ) -> None:
        self.upload = upload
        self.filename = validate_upload_filename(filename)
        self.content_type = validate_upload_content_type(content_type)
        self.boundary = "notion-upload-" + secrets.token_hex(16)
        self._preamble = (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{self.filename}"\r\n'
            f"Content-Type: {self.content_type}\r\n\r\n"
        ).encode("ascii")
        self._closing = f"\r\n--{self.boundary}--\r\n".encode("ascii")
        self.content_length = len(self._preamble) + upload.size + len(self._closing)
        self._used = False

    def __iter__(self):
        if self._used:
            raise McpProtocolError("Upload request body cannot be replayed.")
        self._used = True
        self.upload.revalidate()
        yield self._preamble
        yield from self.upload.iter_chunks(chunk_size=UPLOAD_CHUNK_BYTES)
        yield self._closing


class NoRedirectHandler(request.HTTPRedirectHandler):
    """Turn every redirect into an HTTP error instead of forwarding credentials."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def build_notion_opener() -> Any:
    """Build the HTTP opener used for fixed-host Notion requests."""

    return request.build_opener(NoRedirectHandler())


def _canonicalize_exact_uuid(value: Any) -> str:
    if not isinstance(value, str) or EXACT_UUID_RE.fullmatch(value) is None:
        raise NotionApiError("Notion returned an invalid file upload identifier.")
    identifier = value.replace("-", "").lower()
    return (
        f"{identifier[0:8]}-"
        f"{identifier[8:12]}-"
        f"{identifier[12:16]}-"
        f"{identifier[16:20]}-"
        f"{identifier[20:32]}"
    )


def validate_file_upload(
    payload: dict[str, Any],
    *,
    expected_status: str,
    expected_id: str | None = None,
) -> dict[str, Any]:
    """Validate a Notion file-upload lifecycle response."""

    if not isinstance(payload, dict) or payload.get("object") != "file_upload":
        raise NotionApiError("Notion returned an invalid file upload response.")
    identifier = _canonicalize_exact_uuid(payload.get("id"))
    if expected_status not in {"pending", "uploaded"}:
        raise NotionApiError("File upload lifecycle expectation is invalid.")
    if payload.get("status") != expected_status:
        raise NotionApiError("Notion returned an unexpected file upload lifecycle state.")
    if expected_id is not None and identifier != _canonicalize_exact_uuid(expected_id):
        raise NotionApiError("Notion returned a mismatched file upload identifier.")
    return {"id": identifier, "status": expected_status}


def normalize_position(position: Any) -> dict[str, Any] | None:
    """Validate a modern Notion block insertion position."""

    if position is None:
        return None
    if not isinstance(position, dict):
        raise McpProtocolError("Block position must be an object.")
    if len(json.dumps(position, default=lambda _value: "invalid")) > 512:
        raise McpProtocolError("Block position is too large.")

    position_type = position.get("type")
    if position_type in {"start", "end"}:
        if set(position) != {"type"}:
            raise McpProtocolError("Block position contains unsupported fields.")
        return {"type": position_type}
    if position_type == "after_block":
        if set(position) != {"type", "after_block"}:
            raise McpProtocolError("Block position contains unsupported fields.")
        after_block = position.get("after_block")
        if not isinstance(after_block, dict) or set(after_block) != {"id"}:
            raise McpProtocolError("after_block position requires exactly one block id.")
        try:
            identifier = _canonicalize_exact_uuid(after_block.get("id"))
        except NotionApiError:
            raise McpProtocolError("after_block position requires a valid block id.") from None
        return {"type": "after_block", "after_block": {"id": identifier}}
    raise McpProtocolError("Block position type must be start, end, or after_block.")


def infer_upload_block_type(content_type: str) -> str:
    """Infer a supported Notion block type from a validated MIME type."""

    content_type = validate_upload_content_type(content_type)
    if content_type == "application/pdf":
        return "pdf"
    for prefix, block_type in (
        ("image/", "image"),
        ("audio/", "audio"),
        ("video/", "video"),
    ):
        if content_type.startswith(prefix):
            return block_type
    return "file"


def validate_upload_block_type(block_type: Any) -> str:
    """Validate a supported file-backed Notion block type."""

    if not isinstance(block_type, str) or block_type not in {
        "image",
        "file",
        "pdf",
        "audio",
        "video",
    }:
        raise McpProtocolError("Unsupported file upload block type.")
    return block_type


def validate_upload_caption(caption: Any) -> str | None:
    """Validate optional plain-text caption content before an upload begins."""

    if caption is not None and (
        not isinstance(caption, str) or len(caption) > 2000 or "\x00" in caption
    ):
        raise McpProtocolError("File upload caption is invalid or too long.")
    return caption


def build_file_upload_block(
    block_type: str, file_upload_id: str, caption: str | None = None
) -> dict[str, Any]:
    """Build one file-upload-backed Notion block."""

    block_type = validate_upload_block_type(block_type)
    try:
        identifier = _canonicalize_exact_uuid(file_upload_id)
    except NotionApiError:
        raise McpProtocolError("File upload block requires a valid upload id.") from None
    caption = validate_upload_caption(caption)

    block_payload: dict[str, Any] = {
        "type": "file_upload",
        "file_upload": {"id": identifier},
    }
    if caption:
        block_payload["caption"] = [
            {"type": "text", "text": {"content": caption}}
        ]
    return {
        "object": "block",
        "type": block_type,
        block_type: block_payload,
    }


def slugify(value: str) -> str:
    """Normalize a value for selector matching."""

    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return normalized.strip("-")


def load_dotenv(path: Path) -> None:
    """Load a simple KEY=VALUE env file without external dependencies."""

    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_dotenv_path() -> Path:
    """Resolve the env file path, allowing an external override."""

    override = os.getenv(DOTENV_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return DOTENV_PATH


def parse_workspace_keys(raw_value: str) -> list[str]:
    """Parse a comma-separated workspace key list."""

    keys: list[str] = []
    seen: set[str] = set()
    for chunk in raw_value.split(","):
        candidate = slugify(chunk)
        if not candidate:
            continue
        if candidate in seen:
            raise ConfigError(f"Duplicate workspace key '{candidate}' in {WORKSPACE_KEYS_ENV_VAR}.")
        seen.add(candidate)
        keys.append(candidate)
    if not keys:
        raise ConfigError(
            f"{WORKSPACE_KEYS_ENV_VAR} must list at least one workspace key."
        )
    return keys


def env_key_fragment(key: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", key.upper())


def workspace_name_env_var(key: str) -> str:
    return f"NOTION_WORKSPACE_{env_key_fragment(key)}_NAME"


def workspace_token_env_var(key: str) -> str:
    return f"NOTION_WORKSPACE_{env_key_fragment(key)}_TOKEN"


def workspace_aliases_env_var(key: str) -> str:
    return f"NOTION_WORKSPACE_{env_key_fragment(key)}_ALIASES"


def split_aliases(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return ()
    aliases: list[str] = []
    seen: set[str] = set()
    for chunk in raw_value.split(","):
        alias = chunk.strip()
        if not alias:
            continue
        normalized = slugify(alias)
        if normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(alias)
    return tuple(aliases)


def load_workspace_configs() -> dict[str, WorkspaceConfig]:
    """Load all configured workspace bindings from the normalized env model."""

    load_dotenv(resolve_dotenv_path())

    raw_keys = os.getenv(WORKSPACE_KEYS_ENV_VAR)
    if not raw_keys:
        raise ConfigError(
            "Missing environment variable NOTION_WORKSPACE_KEYS. "
            "Set it to a comma-separated list of workspace keys."
        )

    workspace_keys = parse_workspace_keys(raw_keys)
    configs: dict[str, WorkspaceConfig] = {}
    missing: list[str] = []
    alias_owners: dict[str, str] = {}

    for key in workspace_keys:
        name_var = workspace_name_env_var(key)
        token_var = workspace_token_env_var(key)
        name = os.getenv(name_var)
        token = os.getenv(token_var)
        if not name:
            missing.append(name_var)
        if not token:
            missing.append(token_var)
        if not name or not token:
            continue

        config = WorkspaceConfig(
            key=key,
            name=name,
            token=token,
            extra_aliases=split_aliases(os.getenv(workspace_aliases_env_var(key))),
        )

        for alias in config.aliases:
            owner = alias_owners.get(alias)
            if owner and owner != key:
                raise ConfigError(
                    f"Workspace selector alias '{alias}' is ambiguous between '{owner}' and '{key}'."
                )
            alias_owners[alias] = key

        configs[key] = config

    if missing:
        raise ConfigError("Missing environment variables: " + ", ".join(sorted(missing)))

    return configs


def resolve_workspace(
    selector: str | None, workspaces: dict[str, WorkspaceConfig]
) -> WorkspaceConfig:
    """Resolve a workspace selector to a configured binding."""

    if not selector:
        raise McpProtocolError(
            "Missing required 'workspace'. Use list_workspaces to see available names."
        )

    normalized = slugify(selector)
    for workspace in workspaces.values():
        if normalized in workspace.aliases:
            return workspace

    options = ", ".join(
        sorted(
            {workspace.key for workspace in workspaces.values()}
            | {workspace.name for workspace in workspaces.values()}
        )
    )
    raise McpProtocolError(
        f"Unknown workspace '{selector}'. Available selectors: {options}"
    )


def canonicalize_notion_id(raw_value: str) -> str:
    """Normalize a Notion UUID to the dashed format expected by the API."""

    match = UUID_RE.search(raw_value)
    if not match:
        raise McpProtocolError(
            "Could not find a Notion page ID in the provided value."
        )
    identifier = re.sub(r"[^0-9a-fA-F]", "", match.group(0)).lower()
    return (
        f"{identifier[0:8]}-"
        f"{identifier[8:12]}-"
        f"{identifier[12:16]}-"
        f"{identifier[16:20]}-"
        f"{identifier[20:32]}"
    )


def rich_text_plain(rich_text: list[dict[str, Any]]) -> str:
    """Extract plain text from Notion rich text fragments."""

    return "".join(fragment.get("plain_text", "") for fragment in rich_text)


def format_parent(parent: dict[str, Any]) -> dict[str, Any]:
    """Return a compact parent summary."""

    parent_type = parent.get("type", "unknown")
    summary: dict[str, Any] = {"type": parent_type}
    for key in (
        "page_id",
        "database_id",
        "workspace",
        "block_id",
        "data_source_id",
    ):
        if key in parent:
            summary[key] = parent[key]
    return summary


def simplify_user(user: dict[str, Any]) -> str:
    """Return a human-friendly user label."""

    return user.get("name") or user.get("id", "unknown-user")


def simplify_property_value(prop: dict[str, Any]) -> Any:
    """Reduce Notion property payloads to plain JSON-friendly values."""

    prop_type = prop.get("type")
    if prop_type == "title":
        return rich_text_plain(prop.get("title", []))
    if prop_type == "rich_text":
        return rich_text_plain(prop.get("rich_text", []))
    if prop_type == "number":
        return prop.get("number")
    if prop_type == "select":
        selected = prop.get("select")
        return selected.get("name") if selected else None
    if prop_type == "status":
        status = prop.get("status")
        return status.get("name") if status else None
    if prop_type == "multi_select":
        return [item.get("name") for item in prop.get("multi_select", [])]
    if prop_type == "date":
        return prop.get("date")
    if prop_type == "checkbox":
        return prop.get("checkbox")
    if prop_type == "people":
        return [simplify_user(person) for person in prop.get("people", [])]
    if prop_type == "relation":
        return [item.get("id") for item in prop.get("relation", [])]
    if prop_type == "url":
        return prop.get("url")
    if prop_type == "email":
        return prop.get("email")
    if prop_type == "phone_number":
        return prop.get("phone_number")
    if prop_type == "created_time":
        return prop.get("created_time")
    if prop_type == "last_edited_time":
        return prop.get("last_edited_time")
    if prop_type == "created_by":
        return simplify_user(prop.get("created_by", {}))
    if prop_type == "last_edited_by":
        return simplify_user(prop.get("last_edited_by", {}))
    if prop_type == "formula":
        formula = prop.get("formula", {})
        inner_type = formula.get("type")
        if inner_type:
            return formula.get(inner_type)
        return formula
    if prop_type == "files":
        files: list[dict[str, Any]] = []
        for file_value in prop.get("files", []):
            entry = {"name": file_value.get("name")}
            file_type = file_value.get("type")
            if file_type and file_type in file_value:
                entry["url"] = file_value[file_type].get("url")
            files.append(entry)
        return files
    if prop_type == "rollup":
        rollup = prop.get("rollup", {})
        inner_type = rollup.get("type")
        if inner_type == "array":
            return rollup.get("array", [])
        return rollup.get(inner_type)
    return prop.get(prop_type) if prop_type else prop


def extract_page_title(page: dict[str, Any]) -> str:
    """Find the display title for a Notion page payload."""

    properties = page.get("properties", {})
    for value in properties.values():
        if value.get("type") == "title":
            title = rich_text_plain(value.get("title", []))
            if title:
                return title
    return "Untitled"


def extract_result_title(result: dict[str, Any]) -> str:
    """Find the display title for a search result."""

    if result.get("object") == "page":
        return extract_page_title(result)
    title = rich_text_plain(result.get("title", []))
    return title or "Untitled"


def render_block_markdown(block: dict[str, Any], indent: int = 0) -> list[str]:
    """Render a Notion block to markdown-ish text."""

    block_type = block.get("type", "unsupported")
    payload = block.get(block_type, {})
    prefix = "  " * indent

    if block_type == "paragraph":
        text = rich_text_plain(payload.get("rich_text", []))
        return [prefix + text] if text else []
    if block_type == "heading_1":
        return [prefix + "# " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "heading_2":
        return [prefix + "## " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "heading_3":
        return [prefix + "### " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "bulleted_list_item":
        return [prefix + "- " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "numbered_list_item":
        return [prefix + "1. " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "to_do":
        checked = "x" if payload.get("checked") else " "
        return [prefix + f"- [{checked}] " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "toggle":
        return [prefix + "- " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "quote":
        return [prefix + "> " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "callout":
        return [prefix + "Callout: " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "code":
        text = rich_text_plain(payload.get("rich_text", []))
        language = payload.get("language") or "plain text"
        return [prefix + f"```{language}", text, prefix + "```"]
    if block_type == "divider":
        return [prefix + "---"]
    if block_type == "bookmark":
        return [prefix + "Bookmark: " + (payload.get("url") or "")]
    if block_type == "child_page":
        return [prefix + "Child page: " + payload.get("title", "Untitled child page")]
    if block_type == "table_of_contents":
        return [prefix + "[Table of contents]"]

    text = rich_text_plain(payload.get("rich_text", [])) if isinstance(payload, dict) else ""
    if text:
        return [prefix + text]
    return [prefix + f"[{block_type}]"]


class NotionClient:
    """Thin stdlib wrapper around the Notion REST API."""

    def __init__(self, workspace: WorkspaceConfig, opener: Any | None = None) -> None:
        self.workspace = workspace
        self._opener = opener if opener is not None else build_notion_opener()

    @staticmethod
    def _notion_url(path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
        ):
            raise McpProtocolError("Notion request path is invalid.")
        try:
            parsed_path = parse.urlsplit(path)
        except ValueError:
            raise McpProtocolError("Notion request path is invalid.") from None
        encoded_path = parsed_path.path
        invalid_segment = False
        if re.search(r"%(?![0-9A-Fa-f]{2})", encoded_path):
            invalid_segment = True
        for segment in encoded_path.split("/"):
            decoded_segment = parse.unquote(segment)
            if (
                decoded_segment in {".", ".."}
                or "/" in decoded_segment
                or "\\" in decoded_segment
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in decoded_segment
                )
            ):
                invalid_segment = True
        if (
            parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.fragment
            or invalid_segment
            or "\\" in path
            or "\r" in path
            or "\n" in path
        ):
            raise McpProtocolError("Notion request path is invalid.")
        return NOTION_API_BASE + path

    @staticmethod
    def _read_json_response(response: Any) -> dict[str, Any]:
        raw = response.read(MAX_NOTION_RESPONSE_BYTES + 1)
        if len(raw) > MAX_NOTION_RESPONSE_BYTES:
            raise NotionApiError("Notion returned an oversized response.")
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise NotionApiError("Notion returned an invalid JSON response.") from None
        if not isinstance(payload, dict):
            raise NotionApiError("Notion returned an invalid JSON response.")
        return payload

    def _request_with_body(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        content_type: str | None,
        content_length: int | None,
        notion_version: str,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.workspace.token}",
            "Notion-Version": notion_version,
            "Accept": "application/json",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        req = request.Request(
            self._notion_url(path),
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(
                req, timeout=NOTION_REQUEST_TIMEOUT_SECONDS
            ) as response:
                return self._read_json_response(response)
        except error.HTTPError as exc:
            status = int(exc.code)
            exc.close()
            raise NotionApiError(status=status) from None
        except error.URLError:
            raise NotionApiError("Could not reach the fixed Notion API endpoint.") from None

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        notion_version: str = NOTION_VERSION,
    ) -> dict[str, Any]:
        body: bytes | None = None
        content_type = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        return self._request_with_body(
            method,
            path,
            body,
            content_type=content_type,
            content_length=len(body) if body is not None else None,
            notion_version=notion_version,
        )

    def upload_small_file(
        self,
        upload: SecureUploadFile,
        *,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Create and send one single-part upload through fixed Notion endpoints."""

        safe_filename = validate_upload_filename(filename)
        safe_content_type = validate_upload_content_type(content_type)
        upload.revalidate()
        pending_payload = self.request_json(
            "POST",
            "/file_uploads",
            {
                "mode": "single_part",
                "filename": safe_filename,
                "content_type": safe_content_type,
            },
            notion_version=FILE_UPLOAD_NOTION_VERSION,
        )
        pending = validate_file_upload(pending_payload, expected_status="pending")

        multipart = MultipartFileBody(upload, safe_filename, safe_content_type)
        upload.revalidate()
        uploaded_payload = self._request_with_body(
            "POST",
            f"/file_uploads/{pending['id']}/send",
            multipart,
            content_type=f"multipart/form-data; boundary={multipart.boundary}",
            content_length=multipart.content_length,
            notion_version=FILE_UPLOAD_NOTION_VERSION,
        )
        return validate_file_upload(
            uploaded_payload,
            expected_status="uploaded",
            expected_id=pending["id"],
        )

    def get_self(self) -> dict[str, Any]:
        return self.request_json("GET", "/users/me")

    def search(
        self,
        query: str,
        page_size: int = 10,
        result_type: str = "page",
        start_cursor: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query,
            "page_size": max(1, min(page_size, 100)),
        }
        if result_type in {"page", "database"}:
            payload["filter"] = {"property": "object", "value": result_type}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return self.request_json("POST", "/search", payload)

    def get_page(self, page_id_or_url: str) -> dict[str, Any]:
        page_id = canonicalize_notion_id(page_id_or_url)
        return self.request_json("GET", f"/pages/{page_id}")

    def get_database(self, database_id_or_url: str) -> dict[str, Any]:
        database_id = canonicalize_notion_id(database_id_or_url)
        return self.request_json("GET", f"/databases/{database_id}")

    def query_database(
        self,
        database_id_or_url: str,
        page_size: int = 10,
        start_cursor: str | None = None,
        filter_payload: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        database_id = canonicalize_notion_id(database_id_or_url)
        payload: dict[str, Any] = {
            "page_size": max(1, min(page_size, 100)),
        }
        if start_cursor:
            payload["start_cursor"] = start_cursor
        if filter_payload:
            payload["filter"] = filter_payload
        if sorts:
            payload["sorts"] = sorts
        return self.request_json("POST", f"/databases/{database_id}/query", payload)

    def create_page(
        self,
        parent: dict[str, Any],
        properties: dict[str, Any],
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": parent,
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return self.request_json("POST", "/pages", payload)

    def append_block_children(
        self,
        block_id_or_url: str,
        children: list[dict[str, Any]],
        *,
        position: dict[str, Any] | None = None,
        notion_version: str | None = None,
    ) -> dict[str, Any]:
        block_id = canonicalize_notion_id(block_id_or_url)
        normalized_position = normalize_position(position)
        if notion_version is not None and notion_version not in {
            NOTION_VERSION,
            FILE_UPLOAD_NOTION_VERSION,
        }:
            raise McpProtocolError("Unsupported Notion API version.")
        if normalized_position is not None and notion_version == NOTION_VERSION:
            raise McpProtocolError(
                "Block position requires the modern Notion API version."
            )
        effective_version = notion_version or (
            FILE_UPLOAD_NOTION_VERSION
            if normalized_position is not None
            else NOTION_VERSION
        )
        payload: dict[str, Any] = {"children": children}
        if normalized_position is not None:
            payload["position"] = normalized_position
        return self.request_json(
            "PATCH",
            f"/blocks/{block_id}/children",
            payload,
            notion_version=effective_version,
        )

    def list_block_children(
        self, block_id: str, block_limit: int = 200
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        next_cursor: str | None = None

        while len(collected) < block_limit:
            query = {"page_size": min(100, block_limit - len(collected))}
            if next_cursor:
                query["start_cursor"] = next_cursor
            path = f"/blocks/{block_id}/children"
            if query:
                path += "?" + parse.urlencode(query)
            response = self.request_json("GET", path)
            results = response.get("results", [])
            if not isinstance(results, list):
                break
            collected.extend(results)
            if not response.get("has_more"):
                break
            next_cursor = response.get("next_cursor")
            if not next_cursor:
                break

        return collected[:block_limit]


def recurse_blocks_to_markdown(
    client: NotionClient,
    block_id: str,
    max_blocks: int = 200,
    indent: int = 0,
) -> tuple[list[str], int]:
    """Recursively render a page's block tree up to a block limit."""

    lines: list[str] = []
    consumed = 0
    for block in client.list_block_children(block_id, block_limit=max_blocks):
        if consumed >= max_blocks:
            break
        lines.extend(render_block_markdown(block, indent=indent))
        consumed += 1
        if block.get("has_children") and consumed < max_blocks:
            child_lines, child_consumed = recurse_blocks_to_markdown(
                client,
                block.get("id", ""),
                max_blocks=max_blocks - consumed,
                indent=indent + 1,
            )
            lines.extend(child_lines)
            consumed += child_consumed
    return lines, consumed


def build_search_summary(
    workspace: WorkspaceConfig,
    response: dict[str, Any],
    query: str,
    result_type: str,
) -> dict[str, Any]:
    """Reduce a Notion search response to a smaller summary payload."""

    summarized_results = []
    for result in response.get("results", []):
        summarized_results.append(
            {
                "object": result.get("object"),
                "id": result.get("id"),
                "title": extract_result_title(result),
                "url": result.get("url"),
                "parent": format_parent(result.get("parent", {})),
                "last_edited_time": result.get("last_edited_time"),
            }
        )

    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "query": query,
        "result_type": result_type,
        "count": len(summarized_results),
        "has_more": bool(response.get("has_more")),
        "next_cursor": response.get("next_cursor"),
        "results": summarized_results,
    }


def build_page_summary(
    workspace: WorkspaceConfig,
    page: dict[str, Any],
    content_markdown: str | None,
    rendered_block_count: int,
) -> dict[str, Any]:
    """Reduce a Notion page response plus content into a single payload."""

    simplified_properties = {
        key: simplify_property_value(value)
        for key, value in page.get("properties", {}).items()
    }
    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "page": {
            "id": page.get("id"),
            "title": extract_page_title(page),
            "url": page.get("url"),
            "created_time": page.get("created_time"),
            "last_edited_time": page.get("last_edited_time"),
            "archived": page.get("archived"),
            "in_trash": page.get("in_trash"),
            "parent": format_parent(page.get("parent", {})),
            "properties": simplified_properties,
        },
        "rendered_block_count": rendered_block_count,
        "content_markdown": content_markdown,
    }


def build_database_summary(workspace: WorkspaceConfig, database: dict[str, Any]) -> dict[str, Any]:
    properties = database.get("properties", {})
    simplified_properties = {
        key: {
            "type": value.get("type"),
            "id": value.get("id"),
        }
        for key, value in properties.items()
    }
    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "database": {
            "id": database.get("id"),
            "title": rich_text_plain(database.get("title", [])) or "Untitled",
            "url": database.get("url"),
            "created_time": database.get("created_time"),
            "last_edited_time": database.get("last_edited_time"),
            "archived": database.get("archived"),
            "in_trash": database.get("in_trash"),
            "parent": format_parent(database.get("parent", {})),
            "properties": simplified_properties,
        },
    }


def build_database_query_summary(
    workspace: WorkspaceConfig,
    database: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    summarized_results = []
    for result in response.get("results", []):
        if result.get("object") != "page":
            continue
        summarized_results.append(
            {
                "id": result.get("id"),
                "title": extract_page_title(result),
                "url": result.get("url"),
                "last_edited_time": result.get("last_edited_time"),
                "parent": format_parent(result.get("parent", {})),
                "properties": {
                    key: simplify_property_value(value)
                    for key, value in result.get("properties", {}).items()
                },
            }
        )
    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "database": {
            "id": database.get("id"),
            "title": rich_text_plain(database.get("title", [])) or "Untitled",
            "url": database.get("url"),
        },
        "count": len(summarized_results),
        "has_more": bool(response.get("has_more")),
        "next_cursor": response.get("next_cursor"),
        "results": summarized_results,
    }


def make_tool_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a tool response as MCP text content."""

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        ]
    }


def tool_list_workspaces(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the configured workspaces and optional token validation."""

    workspaces = load_workspace_configs()
    validate = bool(arguments.get("validate_tokens", False))
    summaries = []
    for workspace in workspaces.values():
        summary = {
            "key": workspace.key,
            "name": workspace.name,
            "aliases": sorted(workspace.aliases),
        }
        if validate:
            try:
                user = NotionClient(workspace).get_self()
                summary["token_status"] = "ok"
                summary["bot_user_id"] = user.get("id")
                summary["bot_name"] = user.get("name")
                summary["bot_type"] = user.get("type")
            except Exception as exc:  # noqa: BLE001
                summary["token_status"] = "error"
                summary["error"] = safe_error_record(exc)
        summaries.append(summary)
    return {
        "workspace_count": len(summaries),
        "workspaces": summaries,
        "read_only_tools": [
            "list_workspaces",
            "search",
            "fetch_page",
            "fetch_database",
            "query_database",
        ],
        "write_tools": [
            "create_page",
            "append_block_children",
            "upload_file",
            "upload_and_append_file_block",
        ],
    }


def tool_search(arguments: dict[str, Any]) -> dict[str, Any]:
    """Search a specific configured Notion workspace."""

    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    query = (arguments.get("query") or "").strip()
    if not query:
        raise McpProtocolError("search requires a non-empty 'query'.")

    page_size = int(arguments.get("page_size", 10))
    result_type = str(arguments.get("result_type", "page")).strip().lower() or "page"
    if result_type not in {"page", "database", "all"}:
        raise McpProtocolError(
            "search 'result_type' must be one of: page, database, all."
        )

    client = NotionClient(workspace)
    response = client.search(
        query=query,
        page_size=page_size,
        result_type=result_type,
        start_cursor=arguments.get("start_cursor"),
    )
    return build_search_summary(workspace, response, query, result_type)


def tool_fetch_page(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch a page and optionally render its content."""

    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    page_id_or_url = arguments.get("page_id_or_url") or arguments.get("page")
    if not page_id_or_url:
        raise McpProtocolError(
            "fetch_page requires 'page_id_or_url' with a page UUID or Notion URL."
        )

    include_content = bool(arguments.get("include_content", True))
    block_limit = int(arguments.get("block_limit", 200))
    block_limit = max(1, min(block_limit, 500))

    client = NotionClient(workspace)
    page = client.get_page(str(page_id_or_url))

    content_markdown = None
    rendered_block_count = 0
    if include_content:
        lines, rendered_block_count = recurse_blocks_to_markdown(
            client,
            page.get("id", ""),
            max_blocks=block_limit,
        )
        content_markdown = "\n".join(line for line in lines if line is not None).strip()

    return build_page_summary(
        workspace=workspace,
        page=page,
        content_markdown=content_markdown,
        rendered_block_count=rendered_block_count,
    )


def tool_fetch_database(arguments: dict[str, Any]) -> dict[str, Any]:
    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    database_id_or_url = arguments.get("database_id_or_url") or arguments.get("database")
    if not database_id_or_url:
        raise McpProtocolError(
            "fetch_database requires 'database_id_or_url' with a database UUID or Notion URL."
        )
    client = NotionClient(workspace)
    database = client.get_database(str(database_id_or_url))
    return build_database_summary(workspace, database)


def tool_query_database(arguments: dict[str, Any]) -> dict[str, Any]:
    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    database_id_or_url = arguments.get("database_id_or_url") or arguments.get("database")
    if not database_id_or_url:
        raise McpProtocolError(
            "query_database requires 'database_id_or_url' with a database UUID or Notion URL."
        )
    page_size = int(arguments.get("page_size", 10))
    filter_payload = arguments.get("filter")
    sorts = arguments.get("sorts")
    if filter_payload is not None and not isinstance(filter_payload, dict):
        raise McpProtocolError("query_database 'filter' must be an object.")
    if sorts is not None and not isinstance(sorts, list):
        raise McpProtocolError("query_database 'sorts' must be an array.")
    client = NotionClient(workspace)
    database = client.get_database(str(database_id_or_url))
    response = client.query_database(
        database_id_or_url=str(database_id_or_url),
        page_size=page_size,
        start_cursor=arguments.get("start_cursor"),
        filter_payload=filter_payload,
        sorts=sorts,
    )
    return build_database_query_summary(workspace, database, response)


def tool_create_page(arguments: dict[str, Any]) -> dict[str, Any]:
    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    parent = arguments.get("parent")
    properties = arguments.get("properties")
    children = arguments.get("children")
    if not isinstance(parent, dict):
        raise McpProtocolError("create_page requires 'parent' as an object.")
    if not isinstance(properties, dict):
        raise McpProtocolError("create_page requires 'properties' as an object.")
    if children is not None and not isinstance(children, list):
        raise McpProtocolError("create_page 'children' must be an array when provided.")
    client = NotionClient(workspace)
    page = client.create_page(parent=parent, properties=properties, children=children)
    return build_page_summary(workspace, page, content_markdown=None, rendered_block_count=0)


def tool_append_block_children(arguments: dict[str, Any]) -> dict[str, Any]:
    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    block_id_or_url = arguments.get("block_id_or_url") or arguments.get("page_id_or_url") or arguments.get("block")
    children = arguments.get("children")
    if not block_id_or_url:
        raise McpProtocolError("append_block_children requires 'block_id_or_url'.")
    if not isinstance(children, list) or not children:
        raise McpProtocolError("append_block_children requires a non-empty 'children' array.")
    client = NotionClient(workspace)
    response = client.append_block_children(
        str(block_id_or_url),
        children,
        position=normalize_position(arguments.get("position")),
    )
    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "appended_count": len(response.get("results", [])),
        "results": response.get("results", []),
    }


def tool_upload_file(arguments: dict[str, Any]) -> dict[str, Any]:
    """Upload one approved local file to a selected workspace."""

    if "path" in arguments or not isinstance(arguments.get("file_path"), str):
        raise McpProtocolError("upload_file requires exactly 'file_path'.")
    file_path = arguments["file_path"]
    if not file_path:
        raise McpProtocolError("upload_file requires exactly 'file_path'.")

    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    roots = parse_upload_roots(os.getenv(UPLOAD_ROOTS_ENV_VAR))
    approved = open_approved_upload(file_path, roots)
    with approved:
        filename = validate_upload_filename(
            arguments.get("filename") or approved.local_name
        )
        content_type = validate_upload_content_type(
            arguments.get("content_type")
            or mimetypes.guess_type(filename, strict=True)[0]
            or "application/octet-stream"
        )
        uploaded = NotionClient(workspace).upload_small_file(
            approved,
            filename=filename,
            content_type=content_type,
        )
        return {
            "workspace": workspace.name,
            "workspace_key": workspace.key,
            "file_upload": {
                "id": uploaded["id"],
                "status": uploaded["status"],
                "filename": filename,
                "content_type": content_type,
                "content_length": approved.size,
            },
        }


def tool_upload_and_append_file_block(arguments: dict[str, Any]) -> dict[str, Any]:
    """Upload one approved local file and attach it to a block."""

    if "path" in arguments or not isinstance(arguments.get("file_path"), str):
        raise McpProtocolError(
            "upload_and_append_file_block requires exactly 'file_path'."
        )
    file_path = arguments["file_path"]
    if not file_path:
        raise McpProtocolError(
            "upload_and_append_file_block requires exactly 'file_path'."
        )
    block_id_or_url = arguments.get("block_id_or_url")
    if not isinstance(block_id_or_url, str) or not block_id_or_url:
        raise McpProtocolError(
            "upload_and_append_file_block requires 'block_id_or_url'."
        )

    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    canonical_block_id = canonicalize_notion_id(block_id_or_url)
    requested_block_type = arguments.get("block_type")
    if requested_block_type is not None:
        requested_block_type = validate_upload_block_type(requested_block_type)
    caption = validate_upload_caption(arguments.get("caption"))
    position = normalize_position(arguments.get("position"))
    roots = parse_upload_roots(os.getenv(UPLOAD_ROOTS_ENV_VAR))
    approved = open_approved_upload(file_path, roots)
    with approved:
        filename = validate_upload_filename(
            arguments.get("filename") or approved.local_name
        )
        content_type = validate_upload_content_type(
            arguments.get("content_type")
            or mimetypes.guess_type(filename, strict=True)[0]
            or "application/octet-stream"
        )
        block_type = requested_block_type or infer_upload_block_type(content_type)
        client = NotionClient(workspace)
        uploaded = client.upload_small_file(
            approved,
            filename=filename,
            content_type=content_type,
        )
        block = build_file_upload_block(
            block_type,
            uploaded["id"],
            caption=caption,
        )
        response = client.append_block_children(
            canonical_block_id,
            [block],
            position=position,
            notion_version=FILE_UPLOAD_NOTION_VERSION,
        )
        results = response.get("results")
        appended_count = len(results) if isinstance(results, list) else 0
        return {
            "workspace": workspace.name,
            "workspace_key": workspace.key,
            "file_upload": {
                "id": uploaded["id"],
                "status": uploaded["status"],
                "filename": filename,
                "content_type": content_type,
                "content_length": approved.size,
            },
            "appended_block_type": block_type,
            "appended_count": appended_count,
        }


TOOLS: dict[str, dict[str, Any]] = {
    "list_workspaces": {
        "description": (
            "List the configured Notion workspaces and optional token health."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "validate_tokens": {
                    "type": "boolean",
                    "description": (
                        "When true, call Notion for each workspace to verify the token."
                    ),
                    "default": False,
                }
            },
            "additionalProperties": False,
        },
        "handler": tool_list_workspaces,
    },
    "search": {
        "description": (
            "Search one configured Notion workspace. The workspace selector is required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace selector for one configured workspace or alias."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Search query for Notion content.",
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                },
                "result_type": {
                    "type": "string",
                    "enum": ["page", "database", "all"],
                    "default": "page",
                },
                "start_cursor": {
                    "type": "string",
                    "description": "Optional cursor for the next search page.",
                },
            },
            "required": ["workspace", "query"],
            "additionalProperties": False,
        },
        "handler": tool_search,
    },
    "fetch_page": {
        "description": (
            "Fetch a single page from one configured Notion workspace and render "
            "its content to markdown-like text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace selector for one configured workspace or alias."
                    ),
                },
                "page_id_or_url": {
                    "type": "string",
                    "description": "The Notion page UUID or page URL to fetch.",
                },
                "include_content": {
                    "type": "boolean",
                    "default": True,
                },
                "block_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 200,
                },
            },
            "required": ["workspace", "page_id_or_url"],
            "additionalProperties": False,
        },
        "handler": tool_fetch_page,
    },
    "fetch_database": {
        "description": "Fetch one Notion database from one configured workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Workspace selector for one configured workspace or alias.",
                },
                "database_id_or_url": {
                    "type": "string",
                    "description": "The Notion database UUID or database URL to fetch.",
                },
            },
            "required": ["workspace", "database_id_or_url"],
            "additionalProperties": False,
        },
        "handler": tool_fetch_database,
    },
    "query_database": {
        "description": "Query one Notion database in one configured workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace selector for one configured workspace or alias."},
                "database_id_or_url": {"type": "string", "description": "The Notion database UUID or URL to query."},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                "start_cursor": {"type": "string", "description": "Optional cursor for the next query page."},
                "filter": {"type": "object", "description": "Optional Notion database query filter object."},
                "sorts": {"type": "array", "description": "Optional Notion database query sorts array."},
            },
            "required": ["workspace", "database_id_or_url"],
            "additionalProperties": False,
        },
        "handler": tool_query_database,
    },
    "create_page": {
        "description": "Create one Notion page in one configured workspace under an explicit parent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace selector for one configured workspace or alias."},
                "parent": {"type": "object", "description": "Notion parent object, for example {\"page_id\": ...} or {\"database_id\": ...}."},
                "properties": {"type": "object", "description": "Notion page properties payload."},
                "children": {"type": "array", "description": "Optional initial child block payloads."},
            },
            "required": ["workspace", "parent", "properties"],
            "additionalProperties": False,
        },
        "handler": tool_create_page,
    },
    "append_block_children": {
        "description": "Append child blocks to one page or block in one configured workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace selector for one configured workspace or alias."},
                "block_id_or_url": {"type": "string", "description": "The Notion block or page UUID/URL to append children to."},
                "children": {"type": "array", "description": "Child blocks to append."},
                "position": {
                    "type": "object",
                    "description": (
                        "Optional 2026-03-11 position object: start, end, or after_block."
                    ),
                },
            },
            "required": ["workspace", "block_id_or_url", "children"],
            "additionalProperties": False,
        },
        "handler": tool_append_block_children,
    },
    "upload_file": {
        "description": "Upload one approved local file to a configured workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "file_path": {"type": "string"},
                "filename": {"type": "string"},
                "content_type": {"type": "string"},
            },
            "required": ["workspace", "file_path"],
            "additionalProperties": False,
        },
        "handler": tool_upload_file,
    },
    "upload_and_append_file_block": {
        "description": (
            "Upload one approved local file and attach it to a selected Notion block."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "block_id_or_url": {"type": "string"},
                "file_path": {"type": "string"},
                "block_type": {"type": "string"},
                "caption": {"type": "string"},
                "filename": {"type": "string"},
                "content_type": {"type": "string"},
                "position": {"type": "object"},
            },
            "required": ["workspace", "block_id_or_url", "file_path"],
            "additionalProperties": False,
        },
        "handler": tool_upload_and_append_file_block,
    },
}


def tool_descriptors() -> list[dict[str, Any]]:
    """Return MCP tool descriptors without local handler functions."""

    descriptors = []
    for name, tool in TOOLS.items():
        descriptors.append(
            {
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            }
        )
    return descriptors


def safe_request_id(value: Any) -> Any:
    """Bound an echoed JSON-RPC id so malformed input cannot inflate an error."""

    if value is None:
        return value
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -(2**63) <= value <= 2**63 - 1
    ):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", value):
        return value
    return None


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    """Process one JSON-RPC request."""

    if not isinstance(message, dict):
        return error_response(None, -32600, "Invalid Request.")
    method = message.get("method")
    params = message.get("params", {})
    request_id = safe_request_id(message.get("id"))

    if not isinstance(method, str) or not isinstance(params, dict):
        return error_response(request_id, -32600, "Invalid Request.")

    if method == "notifications/initialized":
        return None
    if method == "ping":
        return success_response(request_id, {})
    if method == "initialize":
        client_protocol = params.get("protocolVersion") or "2024-11-05"
        if not isinstance(client_protocol, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", client_protocol
        ):
            client_protocol = "2024-11-05"
        return success_response(
            request_id,
            {
                "protocolVersion": client_protocol,
                "capabilities": {
                    "tools": {
                        "listChanged": False,
                    }
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        )
    if method == "tools/list":
        return success_response(request_id, {"tools": tool_descriptors()})
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if (
            not isinstance(tool_name, str)
            or len(tool_name) > 128
            or tool_name not in TOOLS
            or not isinstance(arguments, dict)
        ):
            return error_response(request_id, -32602, "Invalid tool call parameters.")
        try:
            payload = TOOLS[tool_name]["handler"](arguments)
            return success_response(request_id, make_tool_text(payload))
        except (ConfigError, McpProtocolError, NotionApiError) as exc:
            return success_response(request_id, safe_tool_error(exc))
        except Exception as exc:  # noqa: BLE001
            write_safe_stderr("tool_call_error", exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": "Internal server error.",
                    "data": safe_error_record(exc),
                },
            }

    return error_response(request_id, -32601, "Method not found.")


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success response."""

    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response."""

    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def read_message() -> dict[str, Any] | None:
    """Read one Content-Length framed JSON-RPC message from stdin."""

    headers: dict[str, str] = {}
    header_count = 0
    while True:
        raw_line = sys.stdin.buffer.readline(MAX_MCP_HEADER_LINE_BYTES + 1)
        if not raw_line:
            return None
        if len(raw_line) > MAX_MCP_HEADER_LINE_BYTES:
            raise McpProtocolError("MCP header line exceeds the size limit.")
        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise McpProtocolError("MCP header encoding is invalid.") from None
        if not line:
            break
        header_count += 1
        if header_count > MAX_MCP_HEADERS:
            raise McpProtocolError("MCP request has too many headers.")
        if ":" not in line:
            raise McpProtocolError("MCP header line is malformed.")
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if not re.fullmatch(r"[a-z0-9-]{1,64}", name) or len(value) > 128:
            raise McpProtocolError("MCP header line is malformed.")
        if name in headers:
            raise McpProtocolError("MCP request contains duplicate headers.")
        headers[name] = value

    if "content-length" not in headers:
        raise McpProtocolError("Missing Content-Length header.")
    content_length = headers["content-length"]
    if not re.fullmatch(r"[0-9]{1,8}", content_length):
        raise McpProtocolError("Content-Length header is invalid.")
    length = int(content_length)
    if not 1 <= length <= MAX_MCP_MESSAGE_BYTES:
        raise McpProtocolError("MCP message exceeds the size limit.")
    body = sys.stdin.buffer.read(length)
    if len(body) != length:
        raise McpProtocolError("MCP message body is incomplete.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise McpProtocolError("MCP message body is invalid JSON.") from None
    if not isinstance(payload, dict):
        raise McpProtocolError("MCP message body must be an object.")
    return payload


def write_message(message: dict[str, Any]) -> None:
    """Write one Content-Length framed JSON-RPC response to stdout."""

    body = json.dumps(message).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def serve_forever() -> int:
    """Run the MCP stdio server loop."""

    try:
        while True:
            message = read_message()
            if message is None:
                return 0
            response = handle_request(message)
            if response is not None and message.get("id") is not None:
                write_message(response)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        write_safe_stderr("server_error", exc)
        return 1


def main() -> int:
    """Entrypoint for the stdio server."""

    return serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
