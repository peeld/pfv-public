#!/usr/bin/env python3
"""
pfv_delete_cli.py — Command-line tool for PFV file deletion and renaming

Provides CLI commands for:
  - Marking files as deleted
  - Restoring (undeleting) files
  - Marking files as renamed
  - Migrating renamed files
  - Listing deleted/renamed files
  - Checking deletion/rename status

Usage:
    pfv_delete_cli.py delete <vdir> [-m MESSAGE] [-r REASON] [--repo PATH]
    pfv_delete_cli.py restore <vdir> [-m MESSAGE] [-v VERSION] [--repo PATH]
    pfv_delete_cli.py rename <vdir> <new_name> [-m MESSAGE] [--repo PATH]
    pfv_delete_cli.py migrate <old> <new> [-m MESSAGE] [--repo PATH]
    pfv_delete_cli.py list-deleted [--repo PATH]
    pfv_delete_cli.py list-renamed [--repo PATH]
    pfv_delete_cli.py status <vdir> [--repo PATH]
    pfv_delete_cli.py history <vdir> [--repo PATH]
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

try:
    from pfv_deletion import (
        mark_deleted, undelete, mark_renamed, migrate_renamed,
        is_deleted, is_renamed, get_renamed_to,
        list_deleted_files, list_renamed_files
    )
    from pfv import log
    from pfv_storage import open_storage
except ImportError as e:
    print(f"Error: Could not import PFV modules: {e}")
    print("Make sure pfv.py, pfv_storage.py, pfv_state.py, and pfv_deletion.py are in Python path")
    sys.exit(1)

import os


class Colors:
    """ANSI color codes for terminal output."""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    GRAY = '\033[90m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_success(msg: str):
    """Print success message."""
    print(f"{Colors.GREEN}✓{Colors.RESET} {msg}")


def print_error(msg: str):
    """Print error message."""
    print(f"{Colors.RED}✗{Colors.RESET} {msg}", file=sys.stderr)


def print_info(msg: str):
    """Print info message."""
    print(f"{Colors.BLUE}ℹ{Colors.RESET} {msg}")


def print_warning(msg: str):
    """Print warning message."""
    print(f"{Colors.YELLOW}⚠{Colors.RESET} {msg}")


def cmd_delete(args):
    """Delete (mark as deleted) a file."""
    try:
        store = open_storage(args.repo)
        
        print_info(f"Marking {args.vdir} as deleted...")
        
        new_version = mark_deleted(
            args.vdir,
            storage=store,
            author=args.author or os.environ.get("USER", "unknown"),
            message=args.message or f"Deleted via CLI",
            reason=args.reason,
            extra_meta={"delete_method": "cli"}
        )
        
        print_success(f"File marked as deleted")
        print(f"  VDir:     {args.vdir}")
        print(f"  Version:  {new_version}")
        print(f"  Message:  {args.message or '(none)'}")
        if args.reason:
            print(f"  Reason:   {args.reason}")
        print(f"  Author:   {args.author or os.environ.get('USER', 'unknown')}")
        
    except Exception as e:
        print_error(f"Failed to delete {args.vdir}: {e}")
        sys.exit(1)


def cmd_restore(args):
    """Restore (undelete) a file."""
    try:
        store = open_storage(args.repo)
        
        # Check if deleted
        if not is_deleted(args.vdir, storage=store):
            print_warning(f"{args.vdir} is not marked as deleted")
            return
        
        print_info(f"Restoring {args.vdir}...")
        
        new_version = undelete(
            args.vdir,
            storage=store,
            author=args.author or os.environ.get("USER", "unknown"),
            message=args.message or "Restored via CLI",
            restore_version=args.version,
            extra_meta={"restore_method": "cli"}
        )
        
        print_success(f"File restored")
        print(f"  VDir:         {args.vdir}")
        print(f"  New Version:  {new_version}")
        if args.version:
            print(f"  From Version: {args.version}")
        print(f"  Message:      {args.message or '(none)'}")
        print(f"  Author:       {args.author or os.environ.get('USER', 'unknown')}")
        
    except Exception as e:
        print_error(f"Failed to restore {args.vdir}: {e}")
        sys.exit(1)


def cmd_rename(args):
    """Mark a file as renamed."""
    try:
        store = open_storage(args.repo)
        
        if args.new_name == args.vdir:
            print_error("New name cannot be the same as current name")
            sys.exit(1)
        
        print_info(f"Marking {args.vdir} as renamed to {args.new_name}...")
        
        new_version = mark_renamed(
            args.vdir,
            args.new_name,
            storage=store,
            author=args.author or os.environ.get("USER", "unknown"),
            message=args.message or f"Renamed to {args.new_name}",
            extra_meta={"rename_method": "cli"}
        )
        
        print_success(f"File marked as renamed")
        print(f"  Old Name:  {args.vdir}")
        print(f"  New Name:  {args.new_name}")
        print(f"  Version:   {new_version}")
        print(f"  Message:   {args.message or '(none)'}")
        print(f"  Author:    {args.author or os.environ.get('USER', 'unknown')}")
        print()
        print_info("To complete the rename, use: migrate <old> <new>")
        
    except Exception as e:
        print_error(f"Failed to rename {args.vdir}: {e}")
        sys.exit(1)


def cmd_migrate(args):
    """Migrate (complete rename) operation."""
    try:
        store = open_storage(args.repo)
        
        print_info(f"Migrating {args.old} to {args.new}...")
        
        new_version = migrate_renamed(
            args.old,
            args.new,
            storage=store,
            author=args.author or os.environ.get("USER", "unknown"),
            message=args.message or f"Migrated to {args.new}",
        )
        
        print_success(f"Migration complete")
        print(f"  Old VDir:  {args.old}")
        print(f"  New VDir:  {args.new}")
        print(f"  New Ver:   {new_version}")
        print(f"  Message:   {args.message or '(none)'}")
        print(f"  Author:    {args.author or os.environ.get('USER', 'unknown')}")
        print()
        print_info(f"Content from {args.old} copied to {args.new}")
        print_info(f"{args.old} is now marked as renamed")
        
    except Exception as e:
        print_error(f"Failed to migrate: {e}")
        sys.exit(1)


def cmd_list_deleted(args):
    """List all deleted files."""
    try:
        store = open_storage(args.repo)
        deleted = list_deleted_files(storage=store)
        
        if not deleted:
            print_info("No deleted files found")
            return
        
        print(f"\n{Colors.BOLD}Deleted Files{Colors.RESET}")
        print("=" * 80)
        
        for item in deleted:
            print(f"\n{Colors.YELLOW}🗑 {item['vdir']}{Colors.RESET}")
            print(f"  Version: {item['version']}")
            print(f"  Author:  {item['author']}")
            print(f"  Deleted: {item['deleted_at']}")
            if item['reason']:
                print(f"  Reason:  {item['reason']}")
            print(f"  Message: {item['message']}")
        
        print(f"\n{Colors.GRAY}Total: {len(deleted)} deleted file(s){Colors.RESET}\n")
        
    except Exception as e:
        print_error(f"Failed to list deleted files: {e}")
        sys.exit(1)


def cmd_list_renamed(args):
    """List all renamed files."""
    try:
        store = open_storage(args.repo)
        renamed = list_renamed_files(storage=store)
        
        if not renamed:
            print_info("No renamed files found")
            return
        
        print(f"\n{Colors.BOLD}Renamed Files{Colors.RESET}")
        print("=" * 80)
        
        for item in renamed:
            print(f"\n{Colors.BLUE}✏ {item['old_vdir']}{Colors.RESET}")
            print(f"  → {item['new_name']}")
            print(f"  Version: {item['version']}")
            print(f"  Author:  {item['author']}")
            print(f"  Renamed: {item['renamed_at']}")
            print(f"  Message: {item['message']}")
        
        print(f"\n{Colors.GRAY}Total: {len(renamed)} renamed file(s){Colors.RESET}\n")
        
    except Exception as e:
        print_error(f"Failed to list renamed files: {e}")
        sys.exit(1)


def cmd_status(args):
    """Check deletion/rename status of a file."""
    try:
        store = open_storage(args.repo)
        
        deleted = is_deleted(args.vdir, storage=store)
        renamed = is_renamed(args.vdir, storage=store)
        new_name = get_renamed_to(args.vdir, storage=store) if renamed else None
        
        print(f"\n{Colors.BOLD}File Status: {args.vdir}{Colors.RESET}")
        print("=" * 80)
        
        if deleted:
            print(f"  Status:   {Colors.RED}DELETED{Colors.RESET}")
        elif renamed:
            print(f"  Status:   {Colors.BLUE}RENAMED{Colors.RESET}")
        else:
            print(f"  Status:   {Colors.GREEN}ACTIVE{Colors.RESET}")
        
        if renamed and new_name:
            print(f"  New Name: {new_name}")
        
        # Show version info
        info = log(args.vdir, store)
        print(f"\n{Colors.BOLD}Version Information{Colors.RESET}")
        print(f"  Latest: {info.latest}")
        print(f"  HEAD:   {info.head}")
        
        # Show last few versions
        print(f"\n{Colors.BOLD}Recent Versions{Colors.RESET}")
        for slot in reversed(info.slots[-3:]):
            status_icon = "🗑" if slot.is_deleted else "✏" if slot.meta.get("renamed") else "✓"
            print(f"  v{slot.version}: {status_icon} {slot.meta.get('message', '(no message)')}")
        
        print()
        
    except Exception as e:
        print_error(f"Failed to check status: {e}")
        sys.exit(1)


def cmd_history(args):
    """Show detailed history of a file."""
    try:
        store = open_storage(args.repo)
        info = log(args.vdir, store)
        
        print(f"\n{Colors.BOLD}File History: {args.vdir}{Colors.RESET}")
        print("=" * 80)
        
        for slot in sorted(info.slots, key=lambda s: s.version):
            status = ""
            if slot.is_deleted:
                status = f"{Colors.RED}DELETED{Colors.RESET}"
            elif slot.meta.get("renamed"):
                new = slot.meta.get("new_name", "?")
                status = f"{Colors.BLUE}RENAMED→{new}{Colors.RESET}"
            else:
                status = f"{Colors.GREEN}OK{Colors.RESET}"
            
            print(f"\nv{slot.version}: [{status}]")
            print(f"  Author:   {slot.meta.get('author', 'unknown')}")
            print(f"  Message:  {slot.meta.get('message', '(no message)')}")
            print(f"  Date:     {slot.meta.get('committed_at', '?')}")
            
            if slot.is_deleted:
                reason = slot.meta.get("deletion_reason")
                if reason:
                    print(f"  Reason:   {reason}")
            
            if slot.meta.get("restored_from_version"):
                print(f"  Restored: from v{slot.meta.get('restored_from_version')}")
        
        print()
        
    except Exception as e:
        print_error(f"Failed to show history: {e}")
        sys.exit(1)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="PFV file deletion and renaming command-line tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s delete project.mp4 -m "Archived" -r "Completed Q1"
  %(prog)s restore project.mp4 -m "Reprocessing needed"
  %(prog)s rename old_name.psd new_name.psd
  %(prog)s migrate old_name.psd new_name.psd
  %(prog)s list-deleted
  %(prog)s list-renamed
  %(prog)s status project.mp4
  %(prog)s history project.mp4
        """
    )
    
    # Global options
    parser.add_argument(
        "--repo",
        help="Repository path (default: current directory)",
        default="."
    )
    parser.add_argument(
        "--author",
        help="Author name (default: current user)",
        default=None
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Mark file as deleted")
    delete_parser.add_argument("vdir", help="Versioned file directory")
    delete_parser.add_argument("-m", "--message", help="Deletion message")
    delete_parser.add_argument("-r", "--reason", help="Deletion reason")
    delete_parser.set_defaults(func=cmd_delete)
    
    # Restore command
    restore_parser = subparsers.add_parser("restore", help="Restore deleted file")
    restore_parser.add_argument("vdir", help="Versioned file directory")
    restore_parser.add_argument("-m", "--message", help="Restoration message")
    restore_parser.add_argument("-v", "--version", type=int, help="Version to restore from")
    restore_parser.set_defaults(func=cmd_restore)
    
    # Rename command
    rename_parser = subparsers.add_parser("rename", help="Mark file as renamed")
    rename_parser.add_argument("vdir", help="Current versioned file directory")
    rename_parser.add_argument("new_name", help="New file name")
    rename_parser.add_argument("-m", "--message", help="Rename message")
    rename_parser.set_defaults(func=cmd_rename)
    
    # Migrate command
    migrate_parser = subparsers.add_parser("migrate", help="Complete rename operation")
    migrate_parser.add_argument("old", help="Old versioned file directory")
    migrate_parser.add_argument("new", help="New versioned file directory")
    migrate_parser.add_argument("-m", "--message", help="Migrate message")
    migrate_parser.set_defaults(func=cmd_migrate)
    
    # List deleted command
    list_del_parser = subparsers.add_parser("list-deleted", help="List deleted files")
    list_del_parser.set_defaults(func=cmd_list_deleted)
    
    # List renamed command
    list_ren_parser = subparsers.add_parser("list-renamed", help="List renamed files")
    list_ren_parser.set_defaults(func=cmd_list_renamed)
    
    # Status command
    status_parser = subparsers.add_parser("status", help="Check file status")
    status_parser.add_argument("vdir", help="Versioned file directory")
    status_parser.set_defaults(func=cmd_status)
    
    # History command
    hist_parser = subparsers.add_parser("history", help="Show file history")
    hist_parser.add_argument("vdir", help="Versioned file directory")
    hist_parser.set_defaults(func=cmd_history)
    
    args = parser.parse_args()
    
    if not hasattr(args, 'func'):
        parser.print_help()
        sys.exit(1)
    
    args.func(args)


if __name__ == "__main__":
    main()
