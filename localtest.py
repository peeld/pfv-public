from pathlib import Path
from pfv_collection import WorkspaceCollection
from pfv_storage import open_storage
from pfv_state import open_backend
from pfv_recursive import ImportDirectoryOperation

work_tree = Path(r"C:\Users\Al\Documents\Unreal Projects\Robot")
storage   = open_storage(r"C:\Users\Al\Documents\Unreal Projects\Robot_repo")
state     = open_backend(work_tree)             # creates .pfv/meta.json
col       = WorkspaceCollection(work_tree)

op = ImportDirectoryOperation(col, storage=storage, state=state, work_tree=work_tree, skip_existing=True)
for record in op:
    if record is None:
        # existing files skipped
        continue
    print(f"[{op.progress_pct():.1f}%] {record.vdir}")

state.close()
