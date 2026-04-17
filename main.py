import os
import re
import sys
from pathlib import Path

import yt_dlp
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich import print as rprint

console = Console()

DOWNLOADS_FOLDER = Path.home() / "Downloads" / "YT-Downloader"


def is_valid_youtube_url(url: str) -> bool:
    """Validate YouTube URL format."""
    patterns = [
        r"^(https?://)?(www\.)?(youtube\.com/watch\?v=[\w-]{11})",
        r"^(https?://)?(www\.)?(youtu\.be/[\w-]{11})",
        r"^(https?://)?(www\.)?(youtube\.com/shorts/[\w-]{11})",
        r"^(https?://)?(www\.)?(youtube\.com/embed/[\w-]{11})",
    ]
    return any(re.match(pattern, url.strip()) for pattern in patterns)


def _is_better_format(candidate: dict, current: dict | None) -> bool:
    """Prefer higher FPS, then larger known filesize as a tiebreaker."""
    if current is None:
        return True
    candidate_fps = candidate.get("fps") or 0
    current_fps = current.get("fps") or 0
    if candidate_fps != current_fps:
        return candidate_fps > current_fps
    candidate_size = candidate.get("filesize") or 0
    current_size = current.get("filesize") or 0
    return candidate_size > current_size


def fetch_formats(url: str) -> tuple[list[dict], str, int, str]:
    """Fetch all available video+audio formats for the given URL."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])
    title = info.get("title", "Unknown Title")
    duration = info.get("duration", 0)
    uploader = info.get("uploader", "Unknown")

    # Collect unique resolutions and keep best candidates for:
    # 1) streams that already include audio, 2) video-only streams.
    seen: dict[int, dict] = {}
    for f in formats:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        ext = f.get("ext", "")
        fps = f.get("fps")
        filesize = f.get("filesize") or f.get("filesize_approx")

        # Skip audio-only or formats with no resolution
        if vcodec == "none" or height is None:
            continue

        candidate = {
            "format_id": f["format_id"],
            "ext": ext,
            "fps": fps,
            "filesize": filesize,
        }

        bucket = seen.setdefault(
            height,
            {
                "with_audio": None,
                "video_only": None,
            },
        )

        if acodec != "none":
            if _is_better_format(candidate, bucket["with_audio"]):
                bucket["with_audio"] = candidate
        else:
            if _is_better_format(candidate, bucket["video_only"]):
                bucket["video_only"] = candidate

    collected_formats = []
    for height, bucket in seen.items():
        with_audio = bucket["with_audio"]
        video_only = bucket["video_only"]
        if with_audio is None and video_only is None:
            continue

        display_source = with_audio or video_only
        collected_formats.append(
            {
                "height": height,
                "ext": display_source["ext"],
                "fps": display_source["fps"],
                "filesize": display_source["filesize"],
                "with_audio_format_id": with_audio["format_id"] if with_audio else None,
                "video_only_format_id": video_only["format_id"] if video_only else None,
                "has_audio_option": with_audio is not None,
                "has_video_only_option": video_only is not None,
            }
        )

    sorted_formats = sorted(collected_formats, key=lambda x: x["height"], reverse=True)
    return sorted_formats, title, duration, uploader


def format_duration(seconds: int) -> str:
    """Format seconds into mm:ss or hh:mm:ss."""
    if seconds is None:
        return "N/A"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_size(size_bytes) -> str:
    """Format bytes into human-readable size."""
    if not size_bytes:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def download_video(
    url: str,
    height: int,
    output_dir: Path,
    include_audio: bool = True,
    format_id: str | None = None,
) -> None:
    """Download video at the chosen resolution with a Rich progress bar."""
    output_dir.mkdir(parents=True, exist_ok=True)

    progress = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=40),
        "[progress.percentage]{task.percentage:>5.1f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )

    task_id = None

    def progress_hook(d: dict) -> None:
        nonlocal task_id
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if task_id is None and total:
                task_id = progress.add_task("Downloading", total=total)
                progress.start()
            if task_id is not None:
                progress.update(task_id, completed=downloaded, total=total or downloaded)
        elif d["status"] == "finished":
            if task_id is not None:
                progress.update(task_id, completed=progress.tasks[task_id].total)
                progress.stop()
            if include_audio:
                rprint("\n[bold green]Download complete! Merging streams if needed...[/bold green]")
            else:
                rprint("\n[bold green]Download complete![/bold green]")

    # Build selector for either with-audio or video-only mode.
    if format_id:
        format_selector = format_id
    elif include_audio:
        format_selector = (
            f"bestvideo[height={height}]+bestaudio/best[height={height}]"
            f"/best[height<={height}]+bestaudio/best[height<={height}]"
        )
    else:
        format_selector = f"bestvideo[height={height}]/bestvideo[height<={height}]"

    ydl_opts = {
        "format": format_selector,
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    if include_audio:
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def main() -> None:
    console.print(
        Panel.fit(
            "[bold yellow]YouTube Video Downloader[/bold yellow]\n"
            "[dim]Powered by yt-dlp & Rich[/dim]",
            border_style="yellow",
        )
    )

    # ── Step 1: Get URL ──────────────────────────────────────────────────────
    while True:
        url = Prompt.ask("\n[bold cyan]Enter the YouTube URL[/bold cyan]").strip()
        if not url:
            rprint("[red]URL cannot be empty. Please try again.[/red]")
            continue
        if not is_valid_youtube_url(url):
            rprint(
                "[bold red]Invalid YouTube URL.[/bold red] "
                "Accepted formats:\n"
                "  • https://www.youtube.com/watch?v=VIDEO_ID\n"
                "  • https://youtu.be/VIDEO_ID\n"
                "  • https://www.youtube.com/shorts/VIDEO_ID"
            )
            continue
        break

    # ── Step 2: Fetch available formats ─────────────────────────────────────
    with console.status("[bold yellow]Fetching video information...[/bold yellow]"):
        try:
            formats, title, duration, uploader = fetch_formats(url)
        except yt_dlp.utils.DownloadError as e:
            rprint(f"[bold red]Error fetching video info:[/bold red] {e}")
            sys.exit(1)

    if not formats:
        rprint("[bold red]No downloadable video formats found for this URL.[/bold red]")
        sys.exit(1)

    # ── Step 3: Show video info & resolution table ───────────────────────────
    console.print(
        Panel(
            f"[bold white]{title}[/bold white]\n"
            f"[dim]Uploader:[/dim] {uploader}   "
            f"[dim]Duration:[/dim] {format_duration(duration)}",
            title="[green]Video Info[/green]",
            border_style="green",
        )
    )

    table = Table(title="Available Resolutions", border_style="blue", show_lines=True)
    table.add_column("#", style="bold magenta", justify="center", width=4)
    table.add_column("Resolution", style="bold cyan", justify="center")
    table.add_column("FPS", justify="center")
    table.add_column("Approx. Size", justify="right")
    table.add_column("Modes", justify="center")

    for idx, fmt in enumerate(formats, start=1):
        if fmt["has_audio_option"] and fmt["has_video_only_option"]:
            mode_label = "[green]With audio[/green] / [yellow]Video only[/yellow]"
        elif fmt["has_audio_option"]:
            mode_label = "[green]With audio[/green]"
        else:
            mode_label = "[yellow]Video only[/yellow]"

        table.add_row(
            str(idx),
            f"{fmt['height']}p",
            str(fmt["fps"] or "N/A"),
            format_size(fmt["filesize"]),
            mode_label,
        )

    console.print(table)

    # ── Step 4: Ask for resolution choice ────────────────────────────────────
    while True:
        choice = Prompt.ask(
            f"[bold cyan]Select resolution[/bold cyan] (1–{len(formats)})"
        ).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(formats):
            selected = formats[int(choice) - 1]
            break
        rprint(f"[red]Please enter a number between 1 and {len(formats)}.[/red]")

    # Ask whether to download this resolution with audio or video-only.
    if selected["has_audio_option"] and selected["has_video_only_option"]:
        mode_choice = Prompt.ask(
            "[bold cyan]Select mode[/bold cyan] (1: With audio, 2: Video only)",
            choices=["1", "2"],
            default="1",
        )
        include_audio = mode_choice == "1"
    elif selected["has_audio_option"]:
        include_audio = True
    else:
        include_audio = False
        rprint("[yellow]Only video-only stream is available for this resolution.[/yellow]")

    selected_format_id = (
        selected["with_audio_format_id"]
        if include_audio
        else selected["video_only_format_id"]
    )

    chosen_height = selected["height"]
    mode_text = "with audio" if include_audio else "video only (no audio)"
    console.print(
        f"\n[bold green]Downloading:[/bold green] [white]{title}[/white] "
        f"at [bold cyan]{chosen_height}p[/bold cyan] [dim]({mode_text})[/dim]"
    )
    console.print(f"[dim]Saving to:[/dim] [underline]{DOWNLOADS_FOLDER}[/underline]\n")

    # ── Step 5: Download ─────────────────────────────────────────────────────
    try:
        download_video(
            url,
            chosen_height,
            DOWNLOADS_FOLDER,
            include_audio=include_audio,
            format_id=selected_format_id,
        )
    except yt_dlp.utils.DownloadError as e:
        rprint(f"\n[bold red]Download failed:[/bold red] {e}")
        sys.exit(1)

    console.print(
        Panel.fit(
            f"[bold green]Saved to:[/bold green] [underline]{DOWNLOADS_FOLDER}[/underline]",
            title="[green]Done![/green]",
            border_style="green",
        )
    )


if __name__ == "__main__":
    main()
