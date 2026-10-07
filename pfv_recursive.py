"""
pfv_recursive.py -- Recursive operations over a VersionedFileCollection

Provides RecursiveOperation, an abstract base class for walking all entries
in any VersionedFileCollection and applying a per-entry action.

The operation works identically on a PFV repo (RepoCollection) and a local
workspace (WorkspaceCollection) because it programs against the common
VersionedFileCollection interface, not a specific storage mechanism.

Usage pattern
-------------
    op = MyOperation(collection)
    for result in op:
        print(f"{op.progress_pct():.0f}%  {result}")

Or step manually:
    op = MyOperation(collection)
    op.start()
    while not op.done:
        result = op.step()
        pct = op.progress_pct()

Design
------
  RecursiveOperation        abstract base; enumeration, progress tracking, iterator
  process_entry()           must be implemented by subclasses; receives entry name
  PrintEntriesOperation     example; prints each entry name
  IndexOperation            builds CheckoutRecords and optionally seeds a StateBackend
  ImportDirectoryOperation  init + commit a plain directory into a new depot

See pfv_collection.py for the VersionedFileCollection interface and its
RepoCollection / WorkspaceCollection implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator, TYPE_CHECKING

from pfv_collection import VersionedFileCollection

if TYPE_CHECKING:
    from pfv_state import StateBackend
    from pfv_storage import StorageBackend


class RecursiveOperation(ABC):
    """
    Abstract base for operations that walk every entry in a VersionedFileCollection.

    Subclass this and implement process_entry(name) to define what happens at
    each entry. Iteration yields whatever process_entry returns.

    Entries are listed eagerly on start() so progress_pct() is always available.
    """

    def __init__(self, collection: VersionedFileCollection) -> None:
        self._collection = collection
        self._entries: list[str] = []   # populated by start()
        self._index: int = 0            # cursor into _entries
        self._started: bool = False

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def process_entry(self, name: str) -> Any:
        """
        Called once per entry name. Return any value; it becomes the iteration
        result. Raise to abort the walk; the exception propagates to the caller.
        """

    # ------------------------------------------------------------------
    # Progress and state
    # ------------------------------------------------------------------

    @property
    def done(self) -> bool:
        """True once all entries have been processed."""
        return self._started and self._index >= len(self._entries)

    def progress_pct(self) -> float | None:
        """Completion percentage 0-100, or None if start() has not been called yet."""
        if not self._started:
            return None
        total = len(self._entries)
        if total == 0:
            return 100.0
        return 100.0 * self._index / total

    @property
    def current_index(self) -> int:
        """Number of entries processed so far."""
        return self._index

    @property
    def total_entries(self) -> int:
        """Total entries to process (available after start())."""
        return len(self._entries)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Enumerate all entries and reset the cursor. Safe to call again to restart."""
        self._entries = self._collection.list_entries()
        self._index = 0
        self._started = True

    def step(self) -> Any:
        """
        Process the next entry and advance the cursor.
        Raises StopIteration when exhausted. Calls start() automatically if needed.
        """
        if not self._started:
            self.start()
        if self._index >= len(self._entries):
            raise StopIteration
        name = self._entries[self._index]
        result = self.process_entry(name)
        self._index += 1
        return result

    # ------------------------------------------------------------------
    # Iterator protocol; allows `for result in op`
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[Any]:
        self.start()
        return self

    def __next__(self) -> Any:
        if self.done:
            raise StopIteration
        return self.step()


# ---------------------------------------------------------------------------
# Example implementation
# ---------------------------------------------------------------------------

class PrintEntriesOperation(RecursiveOperation):
    """
    Example: walks every entry in the collection and prints its name.
    Yields each entry name string as its result.

    Example usage:
        from pfv_collection import RepoCollection
        from pfv_storage import open_storage
        from pfv_recursive import PrintEntriesOperation

        col = RepoCollection(open_storage("."))
        op  = PrintEntriesOperation(col)
        for name in op:
            print(f"[{op.progress_pct():.1f}%] {name}")
    """

    def process_entry(self, name: str) -> str:
        # Print the entry name and return it so callers can collect results
        print(name)
        return name


# ---------------------------------------------------------------------------
# Index operation; builds CheckoutRecords for any VersionedFileCollection
# ---------------------------------------------------------------------------

class IndexOperation(RecursiveOperation):
    """
    Walks every entry in a VersionedFileCollection and builds a CheckoutRecord
    for each one. Works on both RepoCollection and WorkspaceCollection.

    Records are accumulated in op.indexed and optionally upserted into a
    StateBackend as the walk proceeds (safe to interrupt and resume).

    Parameters
    ----------
    collection : VersionedFileCollection
        The repo or workspace to index.
    work_tree : Path
        Work-tree root stamped onto every CheckoutRecord dest/vdir paths.
    state : StateBackend | None
        If supplied, each record is upserted immediately as it is built.

    Example -- index a repo
    -----------------------
        from pfv_collection import RepoCollection
        from pfv_storage import open_storage
        from pfv_state import open_backend
        from pfv_recursive import IndexOperation

        col   = RepoCollection(open_storage("/mnt/repos"))
        state = open_backend(Path("."), backend="sqlite")
        op    = IndexOperation(col, work_tree=Path("."), state=state)

        for record in op:
            print(f"[{op.progress_pct():.1f}%] {record.vdir}  v{record.version}")

        state.close()
    """

    def __init__(
        self,
        collection: VersionedFileCollection,
        work_tree: Path,
        state: "StateBackend | None" = None,
    ) -> None:
        super().__init__(collection)
        self._work_tree = Path(work_tree).resolve()
        self._state = state
        self.indexed: list[Any] = []  # accumulates CheckoutRecord results

    def process_entry(self, name: str) -> Any:
        """Build a CheckoutRecord for *name* and optionally upsert it into state."""
        from pfv_state import make_record  # local import avoids circular deps

        version = self._collection.get_version(name)
        sha     = self._collection.get_sha(name)

        vdir_path = self._work_tree / name
        record = make_record(
            dest=vdir_path,
            vdir=vdir_path,
            version=version,
            sha_at_checkout=sha,
        )

        if self._state is not None:
            self._state.upsert(record)

        self.indexed.append(record)
        return record


# ---------------------------------------------------------------------------
# Import operation; init + commit workspace files into a new depot
# ---------------------------------------------------------------------------

class ImportDirectoryOperation(RecursiveOperation):
    """
    Walks a WorkspaceCollection, creates a v1 PFV repo entry in storage for
    each file, and records the checkout in state. Turns an ordinary directory
    into a tracked workspace backed by a fresh depot.

    For each entry the operation:
      1. Calls pfv.init(vdir, storage)          -- creates the vdir in the depot
      2. Calls pfv.commit(vdir, file, storage)  -- uploads the file as version 1
      3. Reads the sha written by commit from storage
      4. Upserts a CheckoutRecord into state linking the workspace file to v1

    The file is not moved or copied locally; the workspace path stays in place.
    Only the depot and the state backend are written to.

    Parameters
    ----------
    collection : VersionedFileCollection
        Should be a WorkspaceCollection so entry names are relative file paths.
    storage : StorageBackend
        The depot to create vdirs in. Must be empty or have no conflicting vdirs.
    state : StateBackend
        The workspace state backend to seed with checkout records.
    work_tree : Path
        Absolute root of the workspace (used to resolve entry names to paths).
    author : str | None
        Recorded in the v1 .meta for every committed file.
    message : str | None
        Commit message written to v1 .meta (default: "Initial import").
    skip_existing : bool
        If True, silently skip entries whose vdir already exists in the depot.
        If False (default), raise FileExistsError on collision.

    Example
    -------
        from pathlib import Path
        from pfv_collection import WorkspaceCollection
        from pfv_storage import open_storage
        from pfv_state import open_backend
        from pfv_recursive import ImportDirectoryOperation

        work_tree = Path("/existing/project")
        storage   = open_storage("/new/depot")
        state     = open_backend(work_tree)
        col       = WorkspaceCollection(work_tree)

        op = ImportDirectoryOperation(col, storage=storage, state=state,
                                      work_tree=work_tree, message="Initial import")
        for record in op:
            print(f"[{op.progress_pct():.1f}%] imported {record.vdir} v{record.version}")

        print(f"Done -- {len(op.imported)} imported, {len(op.skipped)} skipped.")
        state.close()
    """

    def __init__(
        self,
        collection: VersionedFileCollection,
        storage: "StorageBackend",
        state: "StateBackend",
        work_tree: Path,
        author: str | None = None,
        message: str | None = None,
        skip_existing: bool = False,
    ) -> None:
        super().__init__(collection)
        self._storage = storage
        self._state = state
        self._work_tree = Path(work_tree).resolve()
        self._author = author
        self._message = message or "Initial import"
        self._skip_existing = skip_existing
        self.imported: list[Any] = []   # successfully imported CheckoutRecords
        self.skipped: list[str] = []    # entry names skipped due to skip_existing

    def process_entry(self, name: str) -> Any:
        """Init + commit one file, write the checkout record, return the record."""
        import pfv
        from pfv_state import make_record

        file_path = self._work_tree / name

        print(f"Process {file_path}")

        # Collision check -- vdir already exists in the depot
        if self._storage.exists(f"{name}/latest"):
            if self._skip_existing:
                self.skipped.append(name)
                return None
            raise FileExistsError(
                f"vdir {name!r} already exists in depot. "
                "Pass skip_existing=True to skip collisions."
            )

        # Create the vdir and commit the file as version 1
        pfv.init(name, storage=self._storage)
        pfv.commit(
            name,
            file_path,
            storage=self._storage,
            author=self._author,
            message=self._message,
        )

        # Read back the sha that commit wrote so the checkout record is accurate
        sha_key = f"{name}/1.sha"
        sha = ""
        if self._storage.exists(sha_key):
            try:
                raw = self._storage.read_text(sha_key).splitlines()[0].strip()
                sha = raw.split(":", 1)[-1] if ":" in raw else raw
            except Exception:
                pass

        # Seed the workspace state; links the on-disk file to depot v1
        record = make_record(
            dest=file_path,
            vdir=self._work_tree / name,
            version=1,
            sha_at_checkout=sha,
        )
        self._state.upsert(record)
        self.imported.append(record)
        return record


# ---------------------------------------------------------------------------
# Quick smoke-test when run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pfv_collection import RepoCollection, WorkspaceCollection
    from pfv_storage import open_storage

    mode = sys.argv[1] if len(sys.argv) > 1 else "repo"
    root = sys.argv[2] if len(sys.argv) > 2 else "."

    if mode == "workspace":
        col = WorkspaceCollection(Path(root))
        label = f"workspace at {root!r}"
    else:
        col = RepoCollection(open_storage(root))
        label = f"repo at {root!r}"

    op = PrintEntriesOperation(col)
    print(f"Walking {label}")
    for name in op:
        pct = op.progress_pct()
        print(f"  [{pct:>5.1f}%] {name}")

    print(f"Done -- {op.total_entries} entries.")
