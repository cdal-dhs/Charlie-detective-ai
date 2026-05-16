#!/usr/bin/env python3
"""Diagnostic IMAP — vérifie combien d'emails ont le flag AgentProcessed."""
import asyncio
import os

from aioimaplib import aioimaplib


async def check_mailbox(host, port, user, password, name):
    client = aioimaplib.IMAP4_SSL(host, port)
    await client.wait_hello_from_server()
    login = await client.login(user, password)
    if login.result != "OK":
        print(f"[{name}] LOGIN FAILED: {login}")
        return

    await client.select("INBOX")

    # Total emails dans INBOX
    status = await client.search("ALL")
    total = len(status.lines[0].split()) if status.lines else 0

    # Emails AVEC le flag AgentProcessed
    flagged = await client.search("KEYWORD AgentProcessed")
    flagged_count = len(flagged.lines[0].split()) if flagged.lines else 0

    # Emails SANS le flag AgentProcessed
    unflagged = await client.search("UNKEYWORD AgentProcessed")
    unflagged_count = len(unflagged.lines[0].split()) if unflagged.lines else 0

    print(f"[{name}] total={total} flagged={flagged_count} unflagged={unflagged_count}")
    await client.logout()


async def main():
    import sys
    if len(sys.argv) < 5:
        print("Usage: python3 check_imap.py HOST PORT USER PASSWORD")
        return

    host, port, user, pwd = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
    await check_mailbox(host, port, user, pwd, user)


if __name__ == "__main__":
    asyncio.run(main())
