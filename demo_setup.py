#!/usr/bin/env python3
"""
demo_setup.py — Set up a demo PFV repository for testing the GUI

This script creates a simple PFV repository with a few versioned files
for testing the GUI application.

Usage:
    python demo_setup.py [--repo /path/to/repo]
"""

import sys
import tempfile
from pathlib import Path
import argparse


def demo_setup(repo_path: Path = None):
    """Create a demo repository with sample files."""
    
    if repo_path is None:
        repo_path = Path(tempfile.gettempdir()) / "pfv_demo"
    else:
        repo_path = Path(repo_path)
    
    repo_path.mkdir(parents=True, exist_ok=True)
    
    print(f"📁 Setting up demo repository at: {repo_path}")
    
    # Import PFV modules
    try:
        from pfv import init, commit
        from pfv_storage import LocalBackend
    except ImportError as e:
        print(f"❌ Error: Could not import PFV modules: {e}")
        print("Make sure pfv.py and pfv_storage.py are in the Python path")
        return None
    
    # Create storage backend
    storage = LocalBackend(repo_path)
    
    # Create work directory with sample files
    work_tree = repo_path / "work"
    work_tree.mkdir(exist_ok=True)
    
    print("\n📝 Creating versioned files...")
    
    # File 1: Video project
    print("  • Creating 'project.mp4'...")
    video_vdir = "project.mp4"
    init(video_vdir, storage=storage, name="Project Video", 
         owner="alice", description="Main project video file")
    
    # Create some versions
    v1_file = work_tree / "project_v1.mp4"
    v1_file.write_bytes(b"Mock video content v1 (fake binary data)\x00" * 100)
    commit(video_vdir, v1_file, storage=storage, 
           author="alice", message="Initial video render")
    
    v2_file = work_tree / "project_v2.mp4"
    v2_file.write_bytes(b"Mock video content v2 (updated binary data)\x00" * 150)
    commit(video_vdir, v2_file, storage=storage,
           author="bob", message="Color graded version", tag="color-graded")
    
    v3_file = work_tree / "project_v3.mp4"
    v3_file.write_bytes(b"Mock video content v3 (final render)\x00" * 200)
    commit(video_vdir, v3_file, storage=storage,
           author="alice", message="Final export", tag="final")
    
    # File 2: Photoshop file
    print("  • Creating 'design.psd'...")
    psd_vdir = "design.psd"
    init(psd_vdir, storage=storage, name="Design File",
         owner="charlie", description="Photoshop design mockups")
    
    psd_v1 = work_tree / "design_v1.psd"
    psd_v1.write_bytes(b"Mock PSD file v1 - Design concepts\x00" * 50)
    commit(psd_vdir, psd_v1, storage=storage,
           author="charlie", message="Initial mockups")
    
    psd_v2 = work_tree / "design_v2.psd"
    psd_v2.write_bytes(b"Mock PSD file v2 - Refined design\x00" * 75)
    commit(psd_vdir, psd_v2, storage=storage,
           author="charlie", message="Design refinements")
    
    psd_v3 = work_tree / "design_v3.psd"
    psd_v3.write_bytes(b"Mock PSD file v3 - Client feedback applied\x00" * 100)
    commit(psd_vdir, psd_v3, storage=storage,
           author="charlie", message="Client feedback integrated", tag="approved")
    
    # File 3: Audio file
    print("  • Creating 'soundtrack.wav'...")
    audio_vdir = "soundtrack.wav"
    init(audio_vdir, storage=storage, name="Audio Soundtrack",
         owner="david", description="Project soundtrack and audio effects")
    
    audio_v1 = work_tree / "soundtrack_v1.wav"
    audio_v1.write_bytes(b"Mock WAV audio v1 - Rough draft\x00" * 30)
    commit(audio_vdir, audio_v1, storage=storage,
           author="david", message="Rough soundtrack draft")
    
    audio_v2 = work_tree / "soundtrack_v2.wav"
    audio_v2.write_bytes(b"Mock WAV audio v2 - Mixed and mastered\x00" * 60)
    commit(audio_vdir, audio_v2, storage=storage,
           author="david", message="Mixing complete", tag="mixed")
    
    # File 4: Document
    print("  • Creating 'notes.txt'...")
    notes_vdir = "notes.txt"
    init(notes_vdir, storage=storage, name="Project Notes",
         owner="alice", description="Shared project notes and specifications")
    
    notes_v1 = work_tree / "notes_v1.txt"
    notes_v1.write_text("Project kickoff - initial requirements\n" * 5)
    commit(notes_vdir, notes_v1, storage=storage,
           author="alice", message="Initial requirements")
    
    notes_v2 = work_tree / "notes_v2.txt"
    notes_v2.write_text("Updated specs after client review\n" * 10)
    commit(notes_vdir, notes_v2, storage=storage,
           author="alice", message="Updated after client meeting")
    
    # Clean up temporary working files
    for f in work_tree.glob("*"):
        f.unlink()
    
    print("\n✅ Demo repository created successfully!")
    print(f"📂 Repository path: {repo_path}")
    print(f"📂 Work tree path: {work_tree}")
    
    print("\n🚀 To launch the GUI:")
    print(f"   python build.py run -- '{repo_path}' '{work_tree}'   (from the pfv repo root)")
    
    return repo_path, work_tree


def main():
    parser = argparse.ArgumentParser(
        description="Set up a demo PFV repository for testing"
    )
    parser.add_argument(
        "--repo",
        help="Path for the repository (default: temp directory)",
        default=None
    )
    
    args = parser.parse_args()
    
    try:
        demo_setup(args.repo)
    except Exception as e:
        print(f"\n❌ Error during setup: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
