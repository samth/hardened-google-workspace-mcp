"""
Google Gmail MCP Tools

This module provides MCP tools for interacting with the Gmail API.
"""

import json
import logging
import asyncio
import base64
import ssl
from html.parser import HTMLParser
from typing import Annotated, Optional, List, Dict, Literal, Any

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import mimetypes
import os

from fastapi import Body
from pydantic import BeforeValidator, Field


def _parse_str_list(v: Any) -> Any:
    """Parse a JSON-encoded list string into a Python list.

    MCP clients may send List[str] parameters as JSON strings rather than
    native arrays.  This validator accepts both forms so that Pydantic
    validation succeeds either way.
    """
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        # Fall back to comma-separated
        return [s.strip() for s in v.split(",") if s.strip()]
    return v


StrList = Annotated[List[str], BeforeValidator(_parse_str_list)]

from auth.service_decorator import require_google_service
from core.utils import handle_http_errors
from core.server import server
from auth.scopes import (
    GMAIL_SEND_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GMAIL_LABELS_SCOPE,
    GMAIL_READONLY_SCOPE,
)

logger = logging.getLogger(__name__)

GMAIL_BATCH_SIZE = 25
GMAIL_REQUEST_DELAY = 0.1
HTML_BODY_TRUNCATE_LIMIT = 50000
GMAIL_METADATA_HEADERS = ["Subject", "From", "To", "Cc", "Message-ID", "Date"]


class _HTMLTextExtractor(HTMLParser):
    """Extract readable text from HTML using stdlib."""

    _BLOCK_TAGS = frozenset({
        "p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ul", "ol", "blockquote", "pre", "table", "tr", "td", "th",
        "section", "article", "header", "footer", "nav",
    })

    def __init__(self):
        super().__init__()
        self._text = []  # list of (string, blockquote_depth) tuples
        self._skip = False
        self._link_href = None
        self._bq_depth = 0

    _HEADING_TAGS = {"h1": "# ", "h2": "## ", "h3": "### ",
                     "h4": "#### ", "h5": "##### ", "h6": "###### "}

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in self._HEADING_TAGS:
            self._text.append(("\n", self._bq_depth))
            self._text.append((self._HEADING_TAGS[tag], self._bq_depth))
        elif tag == "blockquote":
            self._bq_depth += 1
            self._text.append(("\n", self._bq_depth))
        elif tag in self._BLOCK_TAGS:
            self._text.append(("\n", self._bq_depth))
        elif tag == "br":
            self._text.append(("\n", self._bq_depth))
        elif tag == "a":
            href = dict(attrs).get("href", "")
            self._link_href = href if href else None
            if self._text and self._text[-1][0] and not self._text[-1][0][-1:].isspace():
                self._text.append((" ", self._bq_depth))
        elif tag in ("td", "th", "img", "input", "button"):
            if self._text and self._text[-1][0] and not self._text[-1][0][-1:].isspace():
                self._text.append((" ", self._bq_depth))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        elif tag == "a" and self._link_href:
            self._text.append((f" <{self._link_href}>", self._bq_depth))
            self._link_href = None
        elif tag == "blockquote":
            self._bq_depth = max(0, self._bq_depth - 1)
            self._text.append(("\n", self._bq_depth))
        elif tag in self._BLOCK_TAGS:
            self._text.append(("\n", self._bq_depth))

    def handle_data(self, data):
        if not self._skip:
            self._text.append((data, self._bq_depth))

    def get_text(self) -> str:
        # Process fragments into lines, applying blockquote prefixes
        lines_with_depth = []
        current_line = []
        current_depth = 0
        for text, depth in self._text:
            current_depth = depth
            parts = text.split("\n")
            for i, part in enumerate(parts):
                if i > 0:
                    # Newline boundary: flush accumulated line
                    lines_with_depth.append(
                        (" ".join("".join(current_line).split()), current_depth)
                    )
                    current_line = []
                current_line.append(part)
        if current_line:
            lines_with_depth.append(
                (" ".join("".join(current_line).split()), current_depth)
            )

        # Build output with > prefixes and collapse blank lines
        result = []
        blank_count = 0
        for line_text, depth in lines_with_depth:
            if line_text.strip() == "":
                blank_count += 1
                if blank_count <= 2:
                    result.append("")
            else:
                blank_count = 0
                prefix = "> " * depth
                result.append(prefix + line_text)
        return "\n".join(result).strip()


def _html_to_text(html: str) -> str:
    """Convert HTML to readable plain text."""
    try:
        parser = _HTMLTextExtractor()
        parser.feed(html)
        return parser.get_text()
    except Exception:
        return html


def _extract_message_body(payload):
    """
    Helper function to extract plain text body from a Gmail message payload.
    (Maintained for backward compatibility)

    Args:
        payload (dict): The message payload from Gmail API

    Returns:
        str: The plain text body content, or empty string if not found
    """
    bodies = _extract_message_bodies(payload)
    return bodies.get("text", "")


def _extract_message_bodies(payload):
    """
    Helper function to extract both plain text and HTML bodies from a Gmail message payload.

    Args:
        payload (dict): The message payload from Gmail API

    Returns:
        dict: Dictionary with 'text' and 'html' keys containing body content
    """
    text_body = ""
    html_body = ""
    parts = [payload] if "parts" not in payload else payload.get("parts", [])

    part_queue = list(parts)  # Use a queue for BFS traversal of parts
    while part_queue:
        part = part_queue.pop(0)
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")

        if body_data:
            try:
                decoded_data = base64.urlsafe_b64decode(body_data).decode(
                    "utf-8", errors="ignore"
                )
                if mime_type == "text/plain" and not text_body:
                    text_body = decoded_data
                elif mime_type == "text/html" and not html_body:
                    html_body = decoded_data
            except Exception as e:
                logger.warning(f"Failed to decode body part: {e}")

        # Add sub-parts to queue for multipart messages
        if mime_type.startswith("multipart/") and "parts" in part:
            part_queue.extend(part.get("parts", []))

    # Check the main payload if it has body data directly
    if payload.get("body", {}).get("data"):
        try:
            decoded_data = base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="ignore"
            )
            mime_type = payload.get("mimeType", "")
            if mime_type == "text/plain" and not text_body:
                text_body = decoded_data
            elif mime_type == "text/html" and not html_body:
                html_body = decoded_data
        except Exception as e:
            logger.warning(f"Failed to decode main payload body: {e}")

    return {"text": text_body, "html": html_body}


def _format_body_content(text_body: str, html_body: str) -> str:
    """
    Helper function to format message body content with HTML fallback and truncation.
    Detects useless text/plain fallbacks (e.g., "Your client does not support HTML").

    Args:
        text_body: Plain text body content
        html_body: HTML body content

    Returns:
        Formatted body content string
    """
    text_stripped = text_body.strip()
    html_stripped = html_body.strip()

    # Prefer HTML when available -- plain text from senders often has
    # missing spaces from HTML tag boundaries (e.g., "which<em>no</em>" -> "whichno")
    use_html = html_stripped and (
        not text_stripped
        or "<!--" in text_stripped
        or len(html_stripped) > len(text_stripped) * 50
        or len(html_stripped) > 100  # HTML available and non-trivial
    )

    if use_html:
        content = _html_to_text(html_stripped)
        return content
    elif text_stripped:
        return text_body
    else:
        return "[No readable content found]"


def _extract_attachments(payload: dict) -> List[Dict[str, Any]]:
    """
    Extract attachment metadata from a Gmail message payload.

    Args:
        payload: The message payload from Gmail API

    Returns:
        List of attachment dictionaries with filename, mimeType, size, and attachmentId
    """
    attachments = []

    def search_parts(part):
        """Recursively search for attachments in message parts"""
        # Check if this part is an attachment
        if part.get("filename") and part.get("body", {}).get("attachmentId"):
            attachments.append(
                {
                    "filename": part["filename"],
                    "mimeType": part.get("mimeType", "application/octet-stream"),
                    "size": part.get("body", {}).get("size", 0),
                    "attachmentId": part["body"]["attachmentId"],
                }
            )

        # Recursively search sub-parts
        if "parts" in part:
            for subpart in part["parts"]:
                search_parts(subpart)

    # Start searching from the root payload
    search_parts(payload)
    return attachments


def _extract_headers(payload: dict, header_names: List[str]) -> Dict[str, str]:
    """
    Extract specified headers from a Gmail message payload.

    Args:
        payload: The message payload from Gmail API
        header_names: List of header names to extract

    Returns:
        Dict mapping header names to their values
    """
    headers = {}
    target_headers = {name.lower(): name for name in header_names}
    for header in payload.get("headers", []):
        header_name_lower = header["name"].lower()
        if header_name_lower in target_headers:
            # Store using the original requested casing
            headers[target_headers[header_name_lower]] = header["value"]
    return headers


def _prepare_gmail_message(
    subject: str,
    body: str,
    to: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    body_format: Literal["plain", "html"] = "plain",
    from_email: Optional[str] = None,
    attachments: Optional[List[str]] = None,
) -> tuple[str, Optional[str]]:
    """
    Prepare a Gmail message with threading support.

    Args:
        subject: Email subject
        body: Email body content
        to: Optional recipient email address
        cc: Optional CC email address
        bcc: Optional BCC email address
        thread_id: Optional Gmail thread ID to reply within
        in_reply_to: Optional Message-ID of the message being replied to
        references: Optional chain of Message-IDs for proper threading
        body_format: Content type for the email body ('plain' or 'html')
        from_email: Optional sender email address

    Returns:
        Tuple of (raw_message, thread_id) where raw_message is base64 encoded
    """
    # Handle reply subject formatting
    reply_subject = subject
    if in_reply_to and not subject.lower().startswith("re:"):
        reply_subject = f"Re: {subject}"

    # Prepare the email
    normalized_format = body_format.lower()
    if normalized_format not in {"plain", "html"}:
        raise ValueError("body_format must be either 'plain' or 'html'.")

    # Build a multipart message when attachments are present; otherwise a simple
    # text message (backward compatible). Attachments are local file paths.
    if attachments:
        _MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024  # Gmail's ~25MB limit
        total_bytes = 0
        message = MIMEMultipart()
        message.attach(MIMEText(body, normalized_format))
        for path in attachments:
            if not isinstance(path, str) or not os.path.isfile(path):
                raise ValueError(f"Attachment file not found: {path}")
            total_bytes += os.path.getsize(path)
            if total_bytes > _MAX_TOTAL_ATTACHMENT_BYTES:
                raise ValueError(
                    "Total attachment size exceeds 25MB; upload large files to Drive "
                    "and link them in the body instead."
                )
            ctype, encoding = mimetypes.guess_type(path)
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(path, "rb") as fp:
                part = MIMEBase(maintype, subtype)
                part.set_payload(fp.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=os.path.basename(path),
            )
            message.attach(part)
    else:
        message = MIMEText(body, normalized_format)
    message["Subject"] = reply_subject

    # Add sender if provided
    if from_email:
        message["From"] = from_email

    # Add recipients if provided
    if to:
        message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc

    # Add reply headers for threading
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to

    if references:
        message["References"] = references

    # Encode message
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

    return raw_message, thread_id


def _generate_gmail_web_url(item_id: str, account_index: int = 0) -> str:
    """
    Generate Gmail web interface URL for a message or thread ID.
    Uses #all to access messages from any Gmail folder/label (not just inbox).

    Args:
        item_id: Gmail message ID or thread ID
        account_index: Google account index (default 0 for primary account)

    Returns:
        Gmail web interface URL that opens the message/thread in Gmail web interface
    """
    return f"https://mail.google.com/mail/u/{account_index}/#all/{item_id}"


def _format_gmail_results_plain(
    messages: list, query: str, next_page_token: Optional[str] = None
) -> str:
    """Format Gmail search results in clean, LLM-friendly plain text."""
    if not messages:
        return f"No messages found for query: '{query}'"

    lines = [
        f"Found {len(messages)} messages matching '{query}':",
        "",
        "📧 MESSAGES:",
    ]

    for i, msg in enumerate(messages, 1):
        # Handle potential null/undefined message objects
        if not msg or not isinstance(msg, dict):
            lines.extend(
                [
                    f"  {i}. Message: Invalid message data",
                    "     Error: Message object is null or malformed",
                    "",
                ]
            )
            continue

        # Handle potential null/undefined values from Gmail API
        message_id = msg.get("id")
        thread_id = msg.get("threadId")

        # Convert None, empty string, or missing values to "unknown"
        if not message_id:
            message_id = "unknown"
        if not thread_id:
            thread_id = "unknown"

        if message_id != "unknown":
            message_url = _generate_gmail_web_url(message_id)
        else:
            message_url = "N/A"

        if thread_id != "unknown":
            thread_url = _generate_gmail_web_url(thread_id)
        else:
            thread_url = "N/A"

        lines.extend(
            [
                f"  {i}. Message ID: {message_id}",
                f"     Web Link: {message_url}",
                f"     Thread ID: {thread_id}",
                f"     Thread Link: {thread_url}",
                "",
            ]
        )

    lines.extend(
        [
            "💡 USAGE:",
            "  • Pass the Message IDs **as a list** to get_gmail_messages_content_batch()",
            "    e.g. get_gmail_messages_content_batch(message_ids=[...])",
            "  • Pass the Thread IDs to get_gmail_thread_content() (single) or get_gmail_threads_content_batch() (batch)",
        ]
    )

    # Add pagination info if there's a next page
    if next_page_token:
        lines.append("")
        lines.append(
            f"📄 PAGINATION: To get the next page, call search_gmail_messages again with page_token='{next_page_token}'"
        )

    return "\n".join(lines)


@server.tool()
@handle_http_errors("search_gmail_messages", is_read_only=True, service_type="gmail")
@require_google_service("gmail", "gmail_read")
async def search_gmail_messages(
    service,
    query: str,
    user_google_email: str,
    page_size: int = 10,
    page_token: Optional[str] = None,
) -> str:
    """
    Searches messages in a user's Gmail account based on a query.
    Returns both Message IDs and Thread IDs for each found message, along with Gmail web interface links for manual verification.
    Supports pagination via page_token parameter.

    Args:
        query (str): The search query. Supports standard Gmail search operators.
        user_google_email (str): The user's Google email address. Required.
        page_size (int): The maximum number of messages to return. Defaults to 10.
        page_token (Optional[str]): Token for retrieving the next page of results. Use the next_page_token from a previous response.

    Returns:
        str: LLM-friendly structured results with Message IDs, Thread IDs, and clickable Gmail web interface URLs for each found message.
        Includes pagination token if more results are available.
    """
    logger.info(
        f"[search_gmail_messages] Email: '{user_google_email}', Query: '{query}', Page size: {page_size}"
    )

    # Build the API request parameters
    request_params = {"userId": "me", "q": query, "maxResults": page_size}

    # Add page token if provided
    if page_token:
        request_params["pageToken"] = page_token
        logger.info("[search_gmail_messages] Using page_token for pagination")

    response = await asyncio.to_thread(
        service.users().messages().list(**request_params).execute
    )

    # Handle potential null response (but empty dict {} is valid)
    if response is None:
        logger.warning("[search_gmail_messages] Null response from Gmail API")
        return f"No response received from Gmail API for query: '{query}'"

    messages = response.get("messages", [])
    # Additional safety check for null messages array
    if messages is None:
        messages = []

    # Extract next page token for pagination
    next_page_token = response.get("nextPageToken")

    formatted_output = _format_gmail_results_plain(messages, query, next_page_token)

    logger.info(f"[search_gmail_messages] Found {len(messages)} messages")
    if next_page_token:
        logger.info(
            "[search_gmail_messages] More results available (next_page_token present)"
        )
    return formatted_output


@server.tool()
@handle_http_errors(
    "get_gmail_message_content", is_read_only=True, service_type="gmail"
)
@require_google_service("gmail", "gmail_read")
async def get_gmail_message_content(
    service, message_id: str, user_google_email: str
) -> str:
    """
    Retrieves the full content (subject, sender, recipients, plain text body) of a specific Gmail message.

    Args:
        message_id (str): The unique ID of the Gmail message to retrieve.
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: The message details including subject, sender, date, Message-ID, recipients (To, Cc), and body content.
    """
    logger.info(
        f"[get_gmail_message_content] Invoked. Message ID: '{message_id}', Email: '{user_google_email}'"
    )

    logger.info(f"[get_gmail_message_content] Using service for: {user_google_email}")

    # Fetch message metadata first to get headers
    message_metadata = await asyncio.to_thread(
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=GMAIL_METADATA_HEADERS,
        )
        .execute
    )

    headers = _extract_headers(
        message_metadata.get("payload", {}), GMAIL_METADATA_HEADERS
    )
    subject = headers.get("Subject", "(no subject)")
    sender = headers.get("From", "(unknown sender)")
    to = headers.get("To", "")
    cc = headers.get("Cc", "")
    rfc822_msg_id = headers.get("Message-ID", "")

    # Now fetch the full message to get the body parts
    message_full = await asyncio.to_thread(
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="full",  # Request full payload for body
        )
        .execute
    )

    # Extract both text and HTML bodies using enhanced helper function
    payload = message_full.get("payload", {})
    bodies = _extract_message_bodies(payload)
    text_body = bodies.get("text", "")
    html_body = bodies.get("html", "")

    # Format body content with HTML fallback
    body_data = _format_body_content(text_body, html_body)

    # Extract attachment metadata
    attachments = _extract_attachments(payload)

    content_lines = [
        f"Subject: {subject}",
        f"From:    {sender}",
        f"Date:    {headers.get('Date', '(unknown date)')}",
    ]

    if rfc822_msg_id:
        content_lines.append(f"Message-ID: {rfc822_msg_id}")

    if to:
        content_lines.append(f"To:      {to}")
    if cc:
        content_lines.append(f"Cc:      {cc}")

    content_lines.append(f"\n--- BODY ---\n{body_data or '[No text/plain body found]'}")

    # Add attachment information if present
    if attachments:
        content_lines.append("\n--- ATTACHMENTS ---")
        for i, att in enumerate(attachments, 1):
            size_kb = att["size"] / 1024
            content_lines.append(
                f"{i}. {att['filename']} ({att['mimeType']}, {size_kb:.1f} KB)\n"
                f"   Attachment ID: {att['attachmentId']}\n"
                f"   Use get_gmail_attachment_content(message_id='{message_id}', attachment_id='{att['attachmentId']}') to download"
            )

    return "\n".join(content_lines)


@server.tool()
@handle_http_errors(
    "get_gmail_messages_content_batch", is_read_only=True, service_type="gmail"
)
@require_google_service("gmail", "gmail_read")
async def get_gmail_messages_content_batch(
    service,
    message_ids: StrList,
    user_google_email: str,
    format: Literal["full", "metadata"] = "full",
) -> str:
    """
    Retrieves the content of multiple Gmail messages in a single batch request.
    Supports up to 25 messages per batch to prevent SSL connection exhaustion.

    Args:
        message_ids (List[str]): List of Gmail message IDs to retrieve (max 25 per batch).
        user_google_email (str): The user's Google email address. Required.
        format (Literal["full", "metadata"]): Message format. "full" includes body, "metadata" only headers.

    Returns:
        str: A formatted list of message contents. Metadata format includes: subject, sender, date, Message-ID,
        Thread-ID, Labels, In-Inbox (yes/no), Snippet, and recipients (To, Cc). Full format additionally includes body.
    """
    logger.info(
        f"[get_gmail_messages_content_batch] Invoked. Message count: {len(message_ids)}, Email: '{user_google_email}'"
    )

    if not message_ids:
        raise Exception("No message IDs provided")

    output_messages = []

    # Process in smaller chunks to prevent SSL connection exhaustion
    for chunk_start in range(0, len(message_ids), GMAIL_BATCH_SIZE):
        chunk_ids = message_ids[chunk_start : chunk_start + GMAIL_BATCH_SIZE]
        results: Dict[str, Dict] = {}

        def _batch_callback(request_id, response, exception):
            """Callback for batch requests"""
            results[request_id] = {"data": response, "error": exception}

        # Try to use batch API
        try:
            batch = service.new_batch_http_request(callback=_batch_callback)

            for mid in chunk_ids:
                if format == "metadata":
                    req = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=mid,
                            format="metadata",
                            metadataHeaders=GMAIL_METADATA_HEADERS,
                        )
                    )
                else:
                    req = (
                        service.users()
                        .messages()
                        .get(userId="me", id=mid, format="full")
                    )
                batch.add(req, request_id=mid)

            # Execute batch request
            await asyncio.to_thread(batch.execute)

        except Exception as batch_error:
            # Fallback to sequential processing instead of parallel to prevent SSL exhaustion
            logger.warning(
                f"[get_gmail_messages_content_batch] Batch API failed, falling back to sequential processing: {batch_error}"
            )

            async def fetch_message_with_retry(mid: str, max_retries: int = 3):
                """Fetch a single message with exponential backoff retry for SSL errors"""
                for attempt in range(max_retries):
                    try:
                        if format == "metadata":
                            msg = await asyncio.to_thread(
                                service.users()
                                .messages()
                                .get(
                                    userId="me",
                                    id=mid,
                                    format="metadata",
                                    metadataHeaders=GMAIL_METADATA_HEADERS,
                                )
                                .execute
                            )
                        else:
                            msg = await asyncio.to_thread(
                                service.users()
                                .messages()
                                .get(userId="me", id=mid, format="full")
                                .execute
                            )
                        return mid, msg, None
                    except ssl.SSLError as ssl_error:
                        if attempt < max_retries - 1:
                            # Exponential backoff: 1s, 2s, 4s
                            delay = 2**attempt
                            logger.warning(
                                f"[get_gmail_messages_content_batch] SSL error for message {mid} on attempt {attempt + 1}: {ssl_error}. Retrying in {delay}s..."
                            )
                            await asyncio.sleep(delay)
                        else:
                            logger.error(
                                f"[get_gmail_messages_content_batch] SSL error for message {mid} on final attempt: {ssl_error}"
                            )
                            return mid, None, ssl_error
                    except Exception as e:
                        return mid, None, e

            # Process messages sequentially with small delays to prevent connection exhaustion
            for mid in chunk_ids:
                mid_result, msg_data, error = await fetch_message_with_retry(mid)
                results[mid_result] = {"data": msg_data, "error": error}
                # Brief delay between requests to allow connection cleanup
                await asyncio.sleep(GMAIL_REQUEST_DELAY)

        # Process results for this chunk
        for mid in chunk_ids:
            entry = results.get(mid, {"data": None, "error": "No result"})

            if entry["error"]:
                output_messages.append(f"⚠️ Message {mid}: {entry['error']}\n")
            else:
                message = entry["data"]
                if not message:
                    output_messages.append(f"⚠️ Message {mid}: No data returned\n")
                    continue

                # Extract content based on format
                payload = message.get("payload", {})

                if format == "metadata":
                    headers = _extract_headers(payload, GMAIL_METADATA_HEADERS)
                    subject = headers.get("Subject", "(no subject)")
                    sender = headers.get("From", "(unknown sender)")
                    to = headers.get("To", "")
                    cc = headers.get("Cc", "")
                    rfc822_msg_id = headers.get("Message-ID", "")
                    # Top-level fields (not in payload headers)
                    thread_id = message.get("threadId", "")
                    snippet = message.get("snippet", "")
                    label_ids = message.get("labelIds", [])

                    msg_output = (
                        f"Message ID: {mid}\nSubject: {subject}\nFrom: {sender}\n"
                        f"Date: {headers.get('Date', '(unknown date)')}\n"
                    )
                    if rfc822_msg_id:
                        msg_output += f"Message-ID: {rfc822_msg_id}\n"
                    if thread_id:
                        msg_output += f"Thread-ID: {thread_id}\n"
                    if label_ids:
                        msg_output += f"Labels: {', '.join(label_ids)}\n"
                        msg_output += f"In-Inbox: {'yes' if 'INBOX' in label_ids else 'no'}\n"
                    if snippet:
                        msg_output += f"Snippet: {snippet}\n"
                    if to:
                        msg_output += f"To: {to}\n"
                    if cc:
                        msg_output += f"Cc: {cc}\n"
                    msg_output += f"Web Link: {_generate_gmail_web_url(mid)}\n"

                    output_messages.append(msg_output)
                else:
                    # Full format - extract body too
                    headers = _extract_headers(payload, GMAIL_METADATA_HEADERS)
                    subject = headers.get("Subject", "(no subject)")
                    sender = headers.get("From", "(unknown sender)")
                    to = headers.get("To", "")
                    cc = headers.get("Cc", "")
                    rfc822_msg_id = headers.get("Message-ID", "")

                    # Extract both text and HTML bodies using enhanced helper function
                    bodies = _extract_message_bodies(payload)
                    text_body = bodies.get("text", "")
                    html_body = bodies.get("html", "")

                    # Format body content with HTML fallback
                    body_data = _format_body_content(text_body, html_body)

                    msg_output = (
                        f"Message ID: {mid}\nSubject: {subject}\nFrom: {sender}\n"
                        f"Date: {headers.get('Date', '(unknown date)')}\n"
                    )
                    if rfc822_msg_id:
                        msg_output += f"Message-ID: {rfc822_msg_id}\n"

                    if to:
                        msg_output += f"To: {to}\n"
                    if cc:
                        msg_output += f"Cc: {cc}\n"
                    msg_output += (
                        f"Web Link: {_generate_gmail_web_url(mid)}\n\n{body_data}\n"
                    )

                    output_messages.append(msg_output)

    # Combine all messages with separators
    final_output = f"Retrieved {len(message_ids)} messages:\n\n"
    final_output += "\n---\n\n".join(output_messages)

    return final_output


@server.tool()
@handle_http_errors(
    "get_gmail_attachment_content", is_read_only=True, service_type="gmail"
)
@require_google_service("gmail", "gmail_read")
async def get_gmail_attachment_content(
    service,
    message_id: str,
    attachment_id: str,
    user_google_email: str,
) -> str:
    """
    Downloads the content of a specific email attachment.

    Args:
        message_id (str): The ID of the Gmail message containing the attachment.
        attachment_id (str): The ID of the attachment to download.
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: Attachment metadata and base64-encoded content that can be decoded and saved.
    """
    logger.info(
        f"[get_gmail_attachment_content] Invoked. Message ID: '{message_id}', Email: '{user_google_email}'"
    )

    # Download attachment directly without refetching message metadata.
    #
    # Important: Gmail attachment IDs are ephemeral and change between API calls for the
    # same message. If we refetch the message here to get metadata, the new attachment IDs
    # won't match the attachment_id parameter provided by the caller, causing the function
    # to fail. The attachment download endpoint returns size information, and filename/mime
    # type should be obtained from the original message content call that provided this ID.
    try:
        attachment = await asyncio.to_thread(
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute
        )
    except Exception as e:
        logger.error(
            f"[get_gmail_attachment_content] Failed to download attachment: {e}"
        )
        return (
            f"Error: Failed to download attachment. The attachment ID may have changed.\n"
            f"Please fetch the message content again to get an updated attachment ID.\n\n"
            f"Error details: {str(e)}"
        )

    # Format response with attachment data
    size_bytes = attachment.get("size", 0)
    size_kb = size_bytes / 1024 if size_bytes else 0
    base64_data = attachment.get("data", "")

    # Check if we're in stateless mode (can't save files)
    from auth.oauth_config import is_stateless_mode

    if is_stateless_mode():
        result_lines = [
            "Attachment downloaded successfully!",
            f"Message ID: {message_id}",
            f"Size: {size_kb:.1f} KB ({size_bytes} bytes)",
            "\n⚠️ Stateless mode: File storage disabled.",
            "\nBase64-encoded content (first 100 characters shown):",
            f"{base64_data[:100]}...",
            "\nNote: Attachment IDs are ephemeral. Always use IDs from the most recent message fetch.",
        ]
        logger.info(
            f"[get_gmail_attachment_content] Successfully downloaded {size_kb:.1f} KB attachment (stateless mode)"
        )
        return "\n".join(result_lines)

    # Save attachment and generate URL
    try:
        from core.attachment_storage import (
            get_attachment_storage,
            get_attachment_url,
            get_attachment_local_path,
        )

        storage = get_attachment_storage()

        # Try to get filename and mime type from message (optional - attachment IDs are ephemeral)
        filename = None
        mime_type = None
        try:
            # Quick metadata fetch to try to get attachment info
            # Note: This might fail if attachment IDs changed, but worth trying
            message_metadata = await asyncio.to_thread(
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata")
                .execute
            )
            payload = message_metadata.get("payload", {})
            attachments = _extract_attachments(payload)
            for att in attachments:
                if att.get("attachmentId") == attachment_id:
                    filename = att.get("filename")
                    mime_type = att.get("mimeType")
                    break
        except Exception:
            # If we can't get metadata, use defaults
            logger.debug(
                f"Could not fetch attachment metadata for {attachment_id}, using defaults"
            )

        # Save attachment
        file_id = storage.save_attachment(
            base64_data=base64_data, filename=filename, mime_type=mime_type
        )

        # Generate URL and local filesystem path. The URL works for remote
        # clients; the local path is a direct shortcut for clients running
        # on the same host as the server.
        attachment_url = get_attachment_url(file_id)
        local_path = get_attachment_local_path(file_id)

        result_lines = [
            "Attachment downloaded successfully!",
            f"Message ID: {message_id}",
            f"Size: {size_kb:.1f} KB ({size_bytes} bytes)",
            f"\n📎 Download URL: {attachment_url}",
        ]
        if local_path:
            result_lines.append(f"📁 Local path: {local_path}")
        result_lines += [
            "\nThe attachment has been saved and is available at the URL above",
            "(or directly at the local path when the client shares the server's filesystem).",
            "The file will expire after 1 hour.",
            "\nNote: Attachment IDs are ephemeral. Always use IDs from the most recent message fetch.",
        ]

        logger.info(
            f"[get_gmail_attachment_content] Successfully saved {size_kb:.1f} KB attachment as {file_id}"
        )
        return "\n".join(result_lines)

    except Exception as e:
        logger.error(
            f"[get_gmail_attachment_content] Failed to save attachment: {e}",
            exc_info=True,
        )
        # Fallback to showing base64 preview
        result_lines = [
            "Attachment downloaded successfully!",
            f"Message ID: {message_id}",
            f"Size: {size_kb:.1f} KB ({size_bytes} bytes)",
            "\n⚠️ Failed to save attachment file. Showing preview instead.",
            "\nBase64-encoded content (first 100 characters shown):",
            f"{base64_data[:100]}...",
            f"\nError: {str(e)}",
            "\nNote: Attachment IDs are ephemeral. Always use IDs from the most recent message fetch.",
        ]
        return "\n".join(result_lines)


# =============================================================================
# REMOVED FOR SECURITY: send_gmail_message
# This tool was removed to prevent email exfiltration attacks via prompt injection.
# Users can create drafts using draft_gmail_message and send them manually from Gmail.
# =============================================================================


@server.tool()
@handle_http_errors("draft_gmail_message", service_type="gmail")
@require_google_service("gmail", GMAIL_COMPOSE_SCOPE)
async def draft_gmail_message(
    service,
    user_google_email: str,
    subject: str = Body(..., description="Email subject."),
    body: str = Body(..., description="Email body (plain text)."),
    body_format: Literal["plain", "html"] = Body(
        "plain",
        description="Email body format. Use 'plain' for plaintext or 'html' for HTML content.",
    ),
    to: Optional[str] = Body(None, description="Optional recipient email address."),
    cc: Optional[str] = Body(None, description="Optional CC email address."),
    bcc: Optional[str] = Body(None, description="Optional BCC email address."),
    thread_id: Optional[str] = Body(
        None, description="Optional Gmail thread ID to reply within."
    ),
    in_reply_to: Optional[str] = Body(
        None, description="Optional Message-ID of the message being replied to."
    ),
    references: Optional[str] = Body(
        None, description="Optional chain of Message-IDs for proper threading."
    ),
    from_email: Optional[str] = Body(
        None, description="Optional sender email address (e.g. a send-as alias). Defaults to user_google_email."
    ),
    attachments: Optional[List[str]] = Body(
        None, description="Optional list of local file paths to attach (combined size up to ~25MB)."
    ),
) -> str:
    """
    Creates a draft email in the user's Gmail account. Supports both new drafts and reply drafts.

    Args:
        user_google_email (str): The user's Google email address. Required.
        subject (str): Email subject.
        body (str): Email body (plain text).
        body_format (Literal['plain', 'html']): Email body format. Defaults to 'plain'.
        to (Optional[str]): Optional recipient email address. Can be left empty for drafts.
        cc (Optional[str]): Optional CC email address.
        bcc (Optional[str]): Optional BCC email address.
        thread_id (Optional[str]): Optional Gmail thread ID to reply within. When provided, creates a reply draft.
        in_reply_to (Optional[str]): Optional Message-ID of the message being replied to. Used for proper threading.
        references (Optional[str]): Optional chain of Message-IDs for proper threading. Should include all previous Message-IDs.
        from_email (Optional[str]): Optional sender address override (e.g. a send-as alias). Defaults to user_google_email.

    Returns:
        str: Confirmation message with the created draft's ID.

    Examples:
        # Create a new draft
        draft_gmail_message(subject="Hello", body="Hi there!", to="user@example.com")

        # Create a plaintext draft with CC and BCC
        draft_gmail_message(
            subject="Project Update",
            body="Here's the latest update...",
            to="user@example.com",
            cc="manager@example.com",
            bcc="archive@example.com"
        )

        # Create a HTML draft with CC and BCC
        draft_gmail_message(
            subject="Project Update",
            body="<strong>Hi there!</strong>",
            body_format="html",
            to="user@example.com",
            cc="manager@example.com",
            bcc="archive@example.com"
        )

        # Create a reply draft in plaintext
        draft_gmail_message(
            subject="Re: Meeting tomorrow",
            body="Thanks for the update!",
            to="user@example.com",
            thread_id="thread_123",
            in_reply_to="<message123@gmail.com>",
            references="<original@gmail.com> <message123@gmail.com>"
        )

        # Create a reply draft in HTML
        draft_gmail_message(
            subject="Re: Meeting tomorrow",
            body="<strong>Thanks for the update!</strong>",
            body_format="html,
            to="user@example.com",
            thread_id="thread_123",
            in_reply_to="<message123@gmail.com>",
            references="<original@gmail.com> <message123@gmail.com>"
        )
    """
    logger.info(
        f"[draft_gmail_message] Invoked. Email: '{user_google_email}', Subject: '{subject}'"
    )

    # Prepare the email message
    raw_message, thread_id_final = _prepare_gmail_message(
        subject=subject,
        body=body,
        body_format=body_format,
        to=to,
        cc=cc,
        bcc=bcc,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
        from_email=from_email or user_google_email,
        attachments=attachments,
    )

    # Create a draft instead of sending
    draft_body = {"message": {"raw": raw_message}}

    # Associate with thread if provided
    if thread_id_final:
        draft_body["message"]["threadId"] = thread_id_final

    # Create the draft
    created_draft = await asyncio.to_thread(
        service.users().drafts().create(userId="me", body=draft_body).execute
    )
    draft_id = created_draft.get("id")
    return f"Draft created! Draft ID: {draft_id}"


@server.tool()
@handle_http_errors("discard_draft", service_type="gmail")
@require_google_service("gmail", GMAIL_COMPOSE_SCOPE)
async def discard_draft(
    service,
    user_google_email: str,
    draft_id: str,
) -> str:
    """
    Permanently discards (deletes) a Gmail draft. This is irreversible and does not move
    the draft to trash -- it is immediately and permanently removed.

    Args:
        user_google_email (str): The user's Google email address. Required.
        draft_id (str): The ID of the draft to discard (as returned by draft_gmail_message).

    Returns:
        str: Confirmation that the draft was discarded.
    """
    logger.info(
        f"[discard_draft] Invoked. Email: '{user_google_email}', Draft ID: '{draft_id}'"
    )

    await asyncio.to_thread(
        service.users().drafts().delete(userId="me", id=draft_id).execute
    )

    return f"Draft {draft_id} discarded successfully."


@server.tool()
@handle_http_errors("update_gmail_draft", service_type="gmail")
@require_google_service("gmail", GMAIL_COMPOSE_SCOPE)
async def update_gmail_draft(
    service,
    user_google_email: str,
    draft_id: str = Body(..., description="The ID of the draft to update."),
    subject: str = Body(..., description="Email subject."),
    body: str = Body(..., description="Email body (plain text)."),
    body_format: Literal["plain", "html"] = Body(
        "plain",
        description="Email body format. Use 'plain' for plaintext or 'html' for HTML content.",
    ),
    to: Optional[str] = Body(None, description="Optional recipient email address."),
    cc: Optional[str] = Body(None, description="Optional CC email address."),
    bcc: Optional[str] = Body(None, description="Optional BCC email address."),
    thread_id: Optional[str] = Body(
        None, description="Optional Gmail thread ID to reply within."
    ),
    in_reply_to: Optional[str] = Body(
        None, description="Optional Message-ID of the message being replied to."
    ),
    references: Optional[str] = Body(
        None, description="Optional chain of Message-IDs for proper threading."
    ),
    from_email: Optional[str] = Body(
        None, description="Optional sender email address (e.g. a send-as alias). Defaults to user_google_email."
    ),
    attachments: Optional[List[str]] = Body(
        None, description="Optional list of local file paths to attach (combined size up to ~25MB)."
    ),
) -> str:
    """
    Updates an existing draft in the user's Gmail account, replacing its content.

    Args:
        user_google_email (str): The user's Google email address. Required.
        draft_id (str): The ID of the draft to update.
        subject (str): Email subject.
        body (str): Email body.
        body_format (Literal['plain', 'html']): Email body format. Defaults to 'plain'.
        to (Optional[str]): Optional recipient email address.
        cc (Optional[str]): Optional CC email address.
        bcc (Optional[str]): Optional BCC email address.
        thread_id (Optional[str]): Optional Gmail thread ID to reply within.
        in_reply_to (Optional[str]): Optional Message-ID for threading.
        references (Optional[str]): Optional chain of Message-IDs for threading.
        from_email (Optional[str]): Optional sender address override.

    Returns:
        str: Confirmation message with the updated draft's ID.
    """
    logger.info(
        f"[update_gmail_draft] Invoked. Email: '{user_google_email}', Draft ID: '{draft_id}', Subject: '{subject}'"
    )

    raw_message, thread_id_final = _prepare_gmail_message(
        subject=subject,
        body=body,
        body_format=body_format,
        to=to,
        cc=cc,
        bcc=bcc,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
        from_email=from_email or user_google_email,
        attachments=attachments,
    )

    draft_body = {"message": {"raw": raw_message}}

    if thread_id_final:
        draft_body["message"]["threadId"] = thread_id_final

    updated_draft = await asyncio.to_thread(
        service.users()
        .drafts()
        .update(userId="me", id=draft_id, body=draft_body)
        .execute
    )
    updated_id = updated_draft.get("id")
    return f"Draft updated! Draft ID: {updated_id}"


@server.tool()
@handle_http_errors("list_gmail_drafts", service_type="gmail")
@require_google_service("gmail", GMAIL_READONLY_SCOPE)
async def list_gmail_drafts(
    service,
    user_google_email: str,
) -> str:
    """
    Lists all drafts in the user's Gmail account, returning draft IDs and message metadata.

    Args:
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: A formatted list of drafts with their draft IDs, message IDs, subjects, and recipients.
    """
    logger.info(
        f"[list_gmail_drafts] Invoked. Email: '{user_google_email}'"
    )

    drafts = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token

        response = await asyncio.to_thread(
            service.users().drafts().list(**kwargs).execute
        )

        batch = response.get("drafts", [])
        drafts.extend(batch)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if not drafts:
        return "No drafts found."

    result_lines = [f"Found {len(drafts)} draft(s):\n"]

    # Fetch metadata for each draft's message
    for draft in drafts:
        draft_id = draft.get("id", "")
        message = draft.get("message", {})
        message_id = message.get("id", "")
        thread_id = message.get("threadId", "")

        # Get message metadata
        try:
            msg_data = await asyncio.to_thread(
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=GMAIL_METADATA_HEADERS,
                )
                .execute
            )
            headers = {
                h["name"]: h["value"]
                for h in msg_data.get("payload", {}).get("headers", [])
            }
            subject = headers.get("Subject", "(no subject)")
            to = headers.get("To", "(no recipient)")
            date = headers.get("Date", "")
        except Exception:
            subject = "(unable to fetch)"
            to = ""
            date = ""

        result_lines.append(f"  Draft ID: {draft_id}")
        result_lines.append(f"  Message ID: {message_id}")
        result_lines.append(f"  Thread ID: {thread_id}")
        result_lines.append(f"  Subject: {subject}")
        result_lines.append(f"  To: {to}")
        if date:
            result_lines.append(f"  Date: {date}")
        result_lines.append("")

    return "\n".join(result_lines)


def _format_thread_content(thread_data: dict, thread_id: str) -> str:
    """
    Helper function to format thread content from Gmail API response.

    Args:
        thread_data (dict): Thread data from Gmail API
        thread_id (str): Thread ID for display

    Returns:
        str: Formatted thread content
    """
    messages = thread_data.get("messages", [])
    if not messages:
        return f"No messages found in thread '{thread_id}'."

    # Extract thread subject from the first message
    first_message = messages[0]
    first_headers = {
        h["name"]: h["value"]
        for h in first_message.get("payload", {}).get("headers", [])
    }
    thread_subject = first_headers.get("Subject", "(no subject)")

    # Build the thread content
    content_lines = [
        f"Thread ID: {thread_id}",
        f"Subject: {thread_subject}",
        f"Messages: {len(messages)}",
        "",
    ]

    # Process each message in the thread
    for i, message in enumerate(messages, 1):
        # Extract headers
        headers = {
            h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])
        }

        sender = headers.get("From", "(unknown sender)")
        date = headers.get("Date", "(unknown date)")
        subject = headers.get("Subject", "(no subject)")

        # Extract both text and HTML bodies
        payload = message.get("payload", {})
        bodies = _extract_message_bodies(payload)
        text_body = bodies.get("text", "")
        html_body = bodies.get("html", "")

        # Format body content with HTML fallback
        body_data = _format_body_content(text_body, html_body)

        # Add message to content
        content_lines.extend(
            [
                f"=== Message {i} ===",
                f"From: {sender}",
                f"Date: {date}",
            ]
        )

        # Only show subject if it's different from thread subject
        if subject != thread_subject:
            content_lines.append(f"Subject: {subject}")

        content_lines.extend(
            [
                "",
                body_data,
                "",
            ]
        )

    return "\n".join(content_lines)


@server.tool()
@require_google_service("gmail", "gmail_read")
@handle_http_errors("get_gmail_thread_content", is_read_only=True, service_type="gmail")
async def get_gmail_thread_content(
    service, thread_id: str, user_google_email: str
) -> str:
    """
    Retrieves the complete content of a Gmail conversation thread, including all messages.

    Args:
        thread_id (str): The unique ID of the Gmail thread to retrieve.
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: The complete thread content with all messages formatted for reading.
    """
    logger.info(
        f"[get_gmail_thread_content] Invoked. Thread ID: '{thread_id}', Email: '{user_google_email}'"
    )

    # Fetch the complete thread with all messages
    thread_response = await asyncio.to_thread(
        service.users().threads().get(userId="me", id=thread_id, format="full").execute
    )

    return _format_thread_content(thread_response, thread_id)


@server.tool()
@require_google_service("gmail", "gmail_read")
@handle_http_errors(
    "get_gmail_threads_content_batch", is_read_only=True, service_type="gmail"
)
async def get_gmail_threads_content_batch(
    service,
    thread_ids: StrList,
    user_google_email: str,
) -> str:
    """
    Retrieves the content of multiple Gmail threads in a single batch request.
    Supports up to 25 threads per batch to prevent SSL connection exhaustion.

    Args:
        thread_ids (List[str]): A list of Gmail thread IDs to retrieve. The function will automatically batch requests in chunks of 25.
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: A formatted list of thread contents with separators.
    """
    logger.info(
        f"[get_gmail_threads_content_batch] Invoked. Thread count: {len(thread_ids)}, Email: '{user_google_email}'"
    )

    if not thread_ids:
        raise ValueError("No thread IDs provided")

    output_threads = []

    def _batch_callback(request_id, response, exception):
        """Callback for batch requests"""
        results[request_id] = {"data": response, "error": exception}

    # Process in smaller chunks to prevent SSL connection exhaustion
    for chunk_start in range(0, len(thread_ids), GMAIL_BATCH_SIZE):
        chunk_ids = thread_ids[chunk_start : chunk_start + GMAIL_BATCH_SIZE]
        results: Dict[str, Dict] = {}

        # Try to use batch API
        try:
            batch = service.new_batch_http_request(callback=_batch_callback)

            for tid in chunk_ids:
                req = service.users().threads().get(userId="me", id=tid, format="full")
                batch.add(req, request_id=tid)

            # Execute batch request
            await asyncio.to_thread(batch.execute)

        except Exception as batch_error:
            # Fallback to sequential processing instead of parallel to prevent SSL exhaustion
            logger.warning(
                f"[get_gmail_threads_content_batch] Batch API failed, falling back to sequential processing: {batch_error}"
            )

            async def fetch_thread_with_retry(tid: str, max_retries: int = 3):
                """Fetch a single thread with exponential backoff retry for SSL errors"""
                for attempt in range(max_retries):
                    try:
                        thread = await asyncio.to_thread(
                            service.users()
                            .threads()
                            .get(userId="me", id=tid, format="full")
                            .execute
                        )
                        return tid, thread, None
                    except ssl.SSLError as ssl_error:
                        if attempt < max_retries - 1:
                            # Exponential backoff: 1s, 2s, 4s
                            delay = 2**attempt
                            logger.warning(
                                f"[get_gmail_threads_content_batch] SSL error for thread {tid} on attempt {attempt + 1}: {ssl_error}. Retrying in {delay}s..."
                            )
                            await asyncio.sleep(delay)
                        else:
                            logger.error(
                                f"[get_gmail_threads_content_batch] SSL error for thread {tid} on final attempt: {ssl_error}"
                            )
                            return tid, None, ssl_error
                    except Exception as e:
                        return tid, None, e

            # Process threads sequentially with small delays to prevent connection exhaustion
            for tid in chunk_ids:
                tid_result, thread_data, error = await fetch_thread_with_retry(tid)
                results[tid_result] = {"data": thread_data, "error": error}
                # Brief delay between requests to allow connection cleanup
                await asyncio.sleep(GMAIL_REQUEST_DELAY)

        # Process results for this chunk
        for tid in chunk_ids:
            entry = results.get(tid, {"data": None, "error": "No result"})

            if entry["error"]:
                output_threads.append(f"⚠️ Thread {tid}: {entry['error']}\n")
            else:
                thread = entry["data"]
                if not thread:
                    output_threads.append(f"⚠️ Thread {tid}: No data returned\n")
                    continue

                output_threads.append(_format_thread_content(thread, tid))

    # Combine all threads with separators
    header = f"Retrieved {len(thread_ids)} threads:"
    return header + "\n\n" + "\n---\n\n".join(output_threads)


@server.tool()
@handle_http_errors("list_gmail_labels", is_read_only=True, service_type="gmail")
@require_google_service("gmail", "gmail_read")
async def list_gmail_labels(service, user_google_email: str) -> str:
    """
    Lists all labels in the user's Gmail account.

    Args:
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: A formatted list of all labels with their IDs, names, and types.
    """
    logger.info(f"[list_gmail_labels] Invoked. Email: '{user_google_email}'")

    response = await asyncio.to_thread(
        service.users().labels().list(userId="me").execute
    )
    labels = response.get("labels", [])

    if not labels:
        return "No labels found."

    lines = [f"Found {len(labels)} labels:", ""]

    system_labels = []
    user_labels = []

    for label in labels:
        if label.get("type") == "system":
            system_labels.append(label)
        else:
            user_labels.append(label)

    if system_labels:
        lines.append("📂 SYSTEM LABELS:")
        for label in system_labels:
            lines.append(f"  • {label['name']} (ID: {label['id']})")
        lines.append("")

    if user_labels:
        lines.append("🏷️  USER LABELS:")
        for label in user_labels:
            lines.append(f"  • {label['name']} (ID: {label['id']})")

    return "\n".join(lines)


@server.tool()
@handle_http_errors("manage_gmail_label", service_type="gmail")
@require_google_service("gmail", GMAIL_LABELS_SCOPE)
async def manage_gmail_label(
    service,
    user_google_email: str,
    action: Literal["create", "update", "delete"],
    name: Optional[str] = None,
    label_id: Optional[str] = None,
    label_list_visibility: Literal["labelShow", "labelHide"] = "labelShow",
    message_list_visibility: Literal["show", "hide"] = "show",
) -> str:
    """
    Manages Gmail labels: create, update, or delete labels.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (Literal["create", "update", "delete"]): Action to perform on the label.
        name (Optional[str]): Label name. Required for create, optional for update.
        label_id (Optional[str]): Label ID. Required for update and delete operations.
        label_list_visibility (Literal["labelShow", "labelHide"]): Whether the label is shown in the label list.
        message_list_visibility (Literal["show", "hide"]): Whether the label is shown in the message list.

    Returns:
        str: Confirmation message of the label operation.
    """
    logger.info(
        f"[manage_gmail_label] Invoked. Email: '{user_google_email}', Action: '{action}'"
    )

    if action == "create" and not name:
        raise Exception("Label name is required for create action.")

    if action in ["update", "delete"] and not label_id:
        raise Exception("Label ID is required for update and delete actions.")

    if action == "create":
        label_object = {
            "name": name,
            "labelListVisibility": label_list_visibility,
            "messageListVisibility": message_list_visibility,
        }
        created_label = await asyncio.to_thread(
            service.users().labels().create(userId="me", body=label_object).execute
        )
        return f"Label created successfully!\nName: {created_label['name']}\nID: {created_label['id']}"

    elif action == "update":
        current_label = await asyncio.to_thread(
            service.users().labels().get(userId="me", id=label_id).execute
        )

        label_object = {
            "id": label_id,
            "name": name if name is not None else current_label["name"],
            "labelListVisibility": label_list_visibility,
            "messageListVisibility": message_list_visibility,
        }

        updated_label = await asyncio.to_thread(
            service.users()
            .labels()
            .update(userId="me", id=label_id, body=label_object)
            .execute
        )
        return f"Label updated successfully!\nName: {updated_label['name']}\nID: {updated_label['id']}"

    elif action == "delete":
        label = await asyncio.to_thread(
            service.users().labels().get(userId="me", id=label_id).execute
        )
        label_name = label["name"]

        await asyncio.to_thread(
            service.users().labels().delete(userId="me", id=label_id).execute
        )
        return f"Label '{label_name}' (ID: {label_id}) deleted successfully!"


@server.tool()
@handle_http_errors("list_gmail_filters", is_read_only=True, service_type="gmail")
@require_google_service("gmail", "gmail_settings_basic")
async def list_gmail_filters(service, user_google_email: str) -> str:
    """
    Lists all Gmail filters configured in the user's mailbox.

    Args:
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: A formatted list of filters with their criteria and actions.
    """
    logger.info(f"[list_gmail_filters] Invoked. Email: '{user_google_email}'")

    response = await asyncio.to_thread(
        service.users().settings().filters().list(userId="me").execute
    )

    filters = response.get("filter") or response.get("filters") or []

    if not filters:
        return "No filters found."

    lines = [f"Found {len(filters)} filters:", ""]

    for filter_obj in filters:
        filter_id = filter_obj.get("id", "(no id)")
        criteria = filter_obj.get("criteria", {})
        action = filter_obj.get("action", {})

        lines.append(f"🔹 Filter ID: {filter_id}")
        lines.append("  Criteria:")

        criteria_lines = []
        if criteria.get("from"):
            criteria_lines.append(f"From: {criteria['from']}")
        if criteria.get("to"):
            criteria_lines.append(f"To: {criteria['to']}")
        if criteria.get("subject"):
            criteria_lines.append(f"Subject: {criteria['subject']}")
        if criteria.get("query"):
            criteria_lines.append(f"Query: {criteria['query']}")
        if criteria.get("negatedQuery"):
            criteria_lines.append(f"Exclude Query: {criteria['negatedQuery']}")
        if criteria.get("hasAttachment"):
            criteria_lines.append("Has attachment")
        if criteria.get("excludeChats"):
            criteria_lines.append("Exclude chats")
        if criteria.get("size"):
            comparison = criteria.get("sizeComparison", "")
            criteria_lines.append(
                f"Size {comparison or ''} {criteria['size']} bytes".strip()
            )

        if not criteria_lines:
            criteria_lines.append("(none)")

        lines.extend([f"    • {line}" for line in criteria_lines])

        lines.append("  Actions:")
        action_lines = []
        if action.get("forward"):
            action_lines.append(f"Forward to: {action['forward']}")
        if action.get("removeLabelIds"):
            action_lines.append(f"Remove labels: {', '.join(action['removeLabelIds'])}")
        if action.get("addLabelIds"):
            action_lines.append(f"Add labels: {', '.join(action['addLabelIds'])}")

        if not action_lines:
            action_lines.append("(none)")

        lines.extend([f"    • {line}" for line in action_lines])
        lines.append("")

    return "\n".join(lines).rstrip()


# =============================================================================
# REMOVED FOR SECURITY: create_gmail_filter, delete_gmail_filter
# These tools were removed to prevent attackers from creating auto-forwarding
# rules or other filters that could exfiltrate data via prompt injection.
# list_gmail_filters (read-only) is kept for visibility into existing filters.
# =============================================================================


@server.tool()
@handle_http_errors("modify_gmail_message_labels", service_type="gmail")
@require_google_service("gmail", GMAIL_MODIFY_SCOPE)
async def modify_gmail_message_labels(
    service,
    user_google_email: str,
    message_id: str,
    add_label_ids: StrList = Field(
        default=[], description="Label IDs to add to the message."
    ),
    remove_label_ids: StrList = Field(
        default=[], description="Label IDs to remove from the message."
    ),
) -> str:
    """
    Adds or removes labels from a Gmail message.
    To archive an email, remove the INBOX label.

    Args:
        user_google_email (str): The user's Google email address. Required.
        message_id (str): The ID of the message to modify.
        add_label_ids (Optional[List[str]]): List of label IDs to add to the message.
        remove_label_ids (Optional[List[str]]): List of label IDs to remove from the message.

    Returns:
        str: Confirmation message of the label changes applied to the message.
    """
    logger.info(
        f"[modify_gmail_message_labels] Invoked. Email: '{user_google_email}', Message ID: '{message_id}'"
    )

    # CW-MODIFIED: Block TRASH and SPAM labels — trashing/spamming messages is destructive
    # and could be exploited via prompt injection to delete important emails.
    blocked_labels = {"TRASH", "SPAM"}
    for label in add_label_ids:
        if label.upper() in blocked_labels:
            raise Exception(
                f"Adding the {label} label is not allowed. "
                "Trashing and marking messages as spam are blocked for security."
            )

    if not add_label_ids and not remove_label_ids:
        raise Exception(
            "At least one of add_label_ids or remove_label_ids must be provided."
        )

    body = {}
    if add_label_ids:
        body["addLabelIds"] = add_label_ids
    if remove_label_ids:
        body["removeLabelIds"] = remove_label_ids

    await asyncio.to_thread(
        service.users().messages().modify(userId="me", id=message_id, body=body).execute
    )

    actions = []
    if add_label_ids:
        actions.append(f"Added labels: {', '.join(add_label_ids)}")
    if remove_label_ids:
        actions.append(f"Removed labels: {', '.join(remove_label_ids)}")

    return f"Message labels updated successfully!\nMessage ID: {message_id}\n{'; '.join(actions)}"


@server.tool()
@handle_http_errors("batch_modify_gmail_message_labels", service_type="gmail")
@require_google_service("gmail", GMAIL_MODIFY_SCOPE)
async def batch_modify_gmail_message_labels(
    service,
    user_google_email: str,
    message_ids: StrList,
    add_label_ids: StrList = Field(
        default=[], description="Label IDs to add to messages."
    ),
    remove_label_ids: StrList = Field(
        default=[], description="Label IDs to remove from messages."
    ),
) -> str:
    """
    Adds or removes labels from multiple Gmail messages in a single batch request.

    Args:
        user_google_email (str): The user's Google email address. Required.
        message_ids (List[str]): A list of message IDs to modify.
        add_label_ids (Optional[List[str]]): List of label IDs to add to the messages.
        remove_label_ids (Optional[List[str]]): List of label IDs to remove from the messages.

    Returns:
        str: Confirmation message of the label changes applied to the messages.
    """
    logger.info(
        f"[batch_modify_gmail_message_labels] Invoked. Email: '{user_google_email}', Message IDs: '{message_ids}'"
    )

    # CW-MODIFIED: Block TRASH and SPAM labels — trashing/spamming messages is destructive
    # and could be exploited via prompt injection to delete important emails.
    blocked_labels = {"TRASH", "SPAM"}
    for label in add_label_ids:
        if label.upper() in blocked_labels:
            raise Exception(
                f"Adding the {label} label is not allowed. "
                "Trashing and marking messages as spam are blocked for security."
            )

    if not add_label_ids and not remove_label_ids:
        raise Exception(
            "At least one of add_label_ids or remove_label_ids must be provided."
        )

    body = {"ids": message_ids}
    if add_label_ids:
        body["addLabelIds"] = add_label_ids
    if remove_label_ids:
        body["removeLabelIds"] = remove_label_ids

    await asyncio.to_thread(
        service.users().messages().batchModify(userId="me", body=body).execute
    )

    actions = []
    if add_label_ids:
        actions.append(f"Added labels: {', '.join(add_label_ids)}")
    if remove_label_ids:
        actions.append(f"Removed labels: {', '.join(remove_label_ids)}")

    return f"Labels updated for {len(message_ids)} messages: {'; '.join(actions)}"
