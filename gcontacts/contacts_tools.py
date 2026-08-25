"""
Google Contacts (People API) MCP Tools

Read-only access to the user's Google Contacts via the People API.
"""

import asyncio
import logging
from typing import List, Optional

from auth.service_decorator import require_google_service
from core.utils import handle_http_errors
from core.server import server


logger = logging.getLogger(__name__)


# Fields returned for each person. Kept broad since these are the columns
# most callers actually want when looking someone up.
DEFAULT_PERSON_FIELDS = (
    "names,emailAddresses,phoneNumbers,addresses,"
    "organizations,birthdays,nicknames,metadata"
)


def _fmt_person(person: dict) -> str:
    """Format one People API person resource as human-readable multi-line text."""
    lines = []
    names = person.get("names", [])
    if names:
        primary = next(
            (n for n in names if n.get("metadata", {}).get("primary")), names[0]
        )
        display = primary.get("displayName") or primary.get("unstructuredName") or ""
        lines.append(f"Name: {display}")
    for e in person.get("emailAddresses", []) or []:
        val = e.get("value", "")
        typ = e.get("type") or e.get("formattedType") or ""
        lines.append(f"  Email ({typ}): {val}" if typ else f"  Email: {val}")
    for p in person.get("phoneNumbers", []) or []:
        val = p.get("value") or p.get("canonicalForm") or ""
        typ = p.get("type") or p.get("formattedType") or ""
        lines.append(f"  Phone ({typ}): {val}" if typ else f"  Phone: {val}")
    for a in person.get("addresses", []) or []:
        formatted = a.get("formattedValue") or ""
        typ = a.get("type") or a.get("formattedType") or ""
        if formatted:
            single = formatted.replace("\n", ", ")
            lines.append(
                f"  Address ({typ}): {single}" if typ else f"  Address: {single}"
            )
    for o in person.get("organizations", []) or []:
        name = o.get("name") or ""
        title = o.get("title") or ""
        if name or title:
            joined = " -- ".join(x for x in (title, name) if x)
            lines.append(f"  Org: {joined}")
    for b in person.get("birthdays", []) or []:
        d = b.get("date") or {}
        parts = [str(d[k]) for k in ("year", "month", "day") if d.get(k)]
        if parts:
            lines.append(f"  Birthday: {'-'.join(parts)}")
    for n in person.get("nicknames", []) or []:
        val = n.get("value", "")
        if val:
            lines.append(f"  Nickname: {val}")
    rn = person.get("resourceName", "")
    if rn:
        lines.append(f"  resourceName: {rn}")
    return "\n".join(lines) if lines else "(no fields)"


@server.tool()
@handle_http_errors("list_contacts", is_read_only=True, service_type="people")
@require_google_service("people", "contacts_read")
async def list_contacts(
    service,
    user_google_email: str,
    page_size: int = 200,
    sort_order: str = "LAST_MODIFIED_DESCENDING",
) -> str:
    """
    List the user's Google Contacts (connections).

    Args:
        user_google_email (str): The user's Google email address. Required.
        page_size (int): Number of contacts to return (max 1000; default 200).
        sort_order (str): One of LAST_MODIFIED_ASCENDING, LAST_MODIFIED_DESCENDING,
                          FIRST_NAME_ASCENDING, LAST_NAME_ASCENDING.
                          Defaults to LAST_MODIFIED_DESCENDING.

    Returns:
        str: A formatted list of contacts with names, emails, phones, addresses.
    """
    logger.info(
        f"[list_contacts] email={user_google_email!r} page_size={page_size} sort={sort_order}"
    )
    page_size = max(1, min(int(page_size), 1000))
    resp = await asyncio.to_thread(
        lambda: service.people()
        .connections()
        .list(
            resourceName="people/me",
            pageSize=page_size,
            personFields=DEFAULT_PERSON_FIELDS,
            sortOrder=sort_order,
        )
        .execute()
    )
    connections = resp.get("connections", []) or []
    total = resp.get("totalPeople") or resp.get("totalItems") or len(connections)
    if not connections:
        return f"No contacts found for {user_google_email}."
    blocks = [_fmt_person(p) for p in connections]
    header = (
        f"Listed {len(connections)} contact(s) for {user_google_email} "
        f"(server-reported total: {total})."
    )
    return header + "\n\n" + "\n\n".join(blocks)


@server.tool()
@handle_http_errors("search_contacts", is_read_only=True, service_type="people")
@require_google_service("people", "contacts_read")
async def search_contacts(
    service,
    user_google_email: str,
    query: str,
    page_size: int = 30,
) -> str:
    """
    Search the user's Google Contacts by name, email, phone, or other fields.

    The People API requires a warm-up call before the first real search returns
    results; this tool issues that warm-up automatically on cache-miss.

    Args:
        user_google_email (str): The user's Google email address. Required.
        query (str): Text to search for (name, email substring, phone digits, etc.).
        page_size (int): Max results to return (max 30 per API; default 30).

    Returns:
        str: A formatted list of matching contacts.
    """
    logger.info(
        f"[search_contacts] email={user_google_email!r} query={query!r} page_size={page_size}"
    )
    page_size = max(1, min(int(page_size), 30))
    read_mask = DEFAULT_PERSON_FIELDS

    def _do_search():
        return (
            service.people()
            .searchContacts(query=query, pageSize=page_size, readMask=read_mask)
            .execute()
        )

    # First call may return empty while the server warms its cache. Try once,
    # and if empty, issue a warm-up + retry.
    resp = await asyncio.to_thread(_do_search)
    results = resp.get("results", []) or []
    if not results:
        # Warm-up: empty-query call primes the cache for this session.
        try:
            await asyncio.to_thread(
                lambda: service.people()
                .searchContacts(query="", pageSize=1, readMask="names")
                .execute()
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"[search_contacts] warm-up failed: {e}")
        resp = await asyncio.to_thread(_do_search)
        results = resp.get("results", []) or []

    if not results:
        return f"No contacts matched {query!r} for {user_google_email}."
    people: List[dict] = [r.get("person", {}) for r in results if r.get("person")]
    blocks = [_fmt_person(p) for p in people]
    return (
        f"Found {len(people)} contact(s) matching {query!r} for {user_google_email}:\n\n"
        + "\n\n".join(blocks)
    )


@server.tool()
@handle_http_errors("get_contact", is_read_only=True, service_type="people")
@require_google_service("people", "contacts_read")
async def get_contact(
    service,
    user_google_email: str,
    resource_name: str,
) -> str:
    """
    Retrieve one contact by resourceName (e.g. 'people/c1234567890').

    Args:
        user_google_email (str): The user's Google email address. Required.
        resource_name (str): The contact's resourceName as returned by list/search.

    Returns:
        str: A formatted view of the contact.
    """
    logger.info(
        f"[get_contact] email={user_google_email!r} resourceName={resource_name!r}"
    )
    person = await asyncio.to_thread(
        lambda: service.people()
        .get(resourceName=resource_name, personFields=DEFAULT_PERSON_FIELDS)
        .execute()
    )
    return _fmt_person(person)
