# txttospeech.py – PDF Reader with Text-to-Speech playback
# Uses edge_tts (Microsoft Edge cloud voices) for high-quality speech synthesis,
# pygame for MP3 audio playback, pypdf for PDF text extraction, and
# customtkinter for a modern graphical interface.

import customtkinter as ctk        # modern themed tkinter widgets
from tkinter import filedialog, messagebox
from tkinterdnd2 import TkinterDnD, DND_FILES  # drag-and-drop support
import edge_tts                    # async TTS – generates MP3 via Microsoft Edge voices
import asyncio                     # needed to run edge_tts coroutines from threads
import threading                   # background work so the UI stays responsive
import os                          # file path / temp file management
import pygame                      # MP3 audio playback with pause/resume support
from pypdf import PdfReader        # PDF text extraction
from mutagen.mp3 import MP3        # read MP3 duration for progress tracking
import time                        # sleep in the progress-update loop
import re                          # text cleanup and sentence splitting
import tempfile                    # safe temporary file for the generated MP3


# --- Temp file path for the speech audio ---
# A single temp file is reused across playback sessions; it is overwritten
# each time a new chunk is synthesised.
TEMP_AUDIO = os.path.join(tempfile.gettempdir(), "tts_output.mp3")


class PDFReaderTTSApp:
    """Read PDF content aloud with playback controls and a modern GUI."""

    # ── Available edge-tts voices (short-name → display label) ──
    # Only a curated subset is shown in the dropdown for simplicity.
    VOICES = {
        "en-US-GuyNeural": "Guy (US)",
        "en-US-JennyNeural": "Jenny (US)",
        "en-US-AriaNeural": "Aria (US)",
        "en-GB-SoniaNeural": "Sonia (UK)",
        "en-GB-RyanNeural": "Ryan (UK)",
        "en-AU-NatashaNeural": "Natasha (AU)",
        "en-AU-WilliamNeural": "William (AU)",
    }

    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title("PDF Reader with Text-to-Speech")
        self.root.geometry("980x720")
        self.root.minsize(860, 620)

        # Initialise the pygame mixer once for the lifetime of the app.
        pygame.mixer.init()

        # --- Playback state ---
        self.pdf_path: str | None = None        # path of the currently loaded PDF
        self.full_text: str = ""                 # raw extracted text (for preview)
        self.text_chunks: list[str] = []         # one entry per PDF page
        self.current_index: int = 0              # index of the page being played
        self.is_playing: bool = False            # True while audio is actively playing
        self.is_paused: bool = False             # True when user has paused playback
        self.stop_requested: bool = False        # signals the playback thread to exit
        self.playback_thread: threading.Thread | None = None
        self.audio_duration: float = 0.0         # duration (seconds) of current MP3
        self.playback_start_time: float = 0.0    # time.time() when current chunk began
        self.paused_offset: float = 0.0          # seconds already played before pause

        # --- CustomTkinter variables for data-bound widgets ---
        self.voice_var = ctk.StringVar(value="en-US-GuyNeural")
        self.rate_var = ctk.StringVar(value="+0%")    # edge-tts rate string
        self.volume_var = ctk.DoubleVar(value=1.0)    # pygame volume 0.0–1.0

        # Build the full interface first so all widgets exist.
        self._build_ui()

        # Register drag-and-drop after the UI is built (status_label must exist).
        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind('<<Drop>>', self._on_drop)

    def _on_drop(self, event):
        """Handle drag-and-drop of PDF files onto the window."""
        # event.data may contain a list of files (Windows: {filename1} {filename2})
        files = self.root.tk.splitlist(event.data)
        for file_path in files:
            if file_path.lower().endswith('.pdf'):
                self._load_pdf_from_drop(file_path)
                break
            else:
                self.status_label.configure(text="Only PDF files are supported.")

    def _load_pdf_from_drop(self, file_path: str) -> None:
        """Load a PDF file from a drag-and-drop event."""
        self._stop_engine()
        try:
            reader = PdfReader(file_path)
            self.pdf_path = file_path
            self.text_chunks = self._extract_pages(reader)
            if not self.text_chunks:
                raise ValueError("The PDF does not contain readable text.")
            self.full_text = "\n\n".join(self.text_chunks)
            self.current_index = 0
            self.file_label.configure(text=f"Current File: {os.path.basename(file_path)}")
            self.page_label.configure(text=f"Pages: {len(reader.pages)}")
            self._update_position_label()
            self.preview.configure(state="normal")
            self.preview.delete("0.0", "end")
            self.preview.insert("0.0", self.full_text)
            self.preview.configure(state="disabled")
            self.progress_bar.set(0)
            self.status_label.configure(text="PDF loaded via drag-and-drop. Press Play to start reading.")
        except Exception as exc:
            messagebox.showerror("PDF Load Error", f"Unable to load the PDF.\n\n{exc}")
            self.status_label.configure(text="Loading failed.")

    # ──────────────────────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Construct the complete CustomTkinter layout."""
        # Use the dark colour theme by default.
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # --- Title ---
        ctk.CTkLabel(
            self.root, text="PDF Reader with Text-to-Speech",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#ffffff",
            fg_color="#000000",
            corner_radius=0,
        ).pack(fill="x", pady=(14, 6), padx=14)

        # --- Toolbar: primary action buttons ---
        toolbar = ctk.CTkFrame(self.root)
        toolbar.pack(fill="x", padx=14, pady=(0, 8))

        # Each button maps to a public method that handles its action.
        ctk.CTkButton(toolbar, text="Load PDF", width=110, command=self.load_pdf).pack(
            side="left", padx=(10, 6), pady=8)
        ctk.CTkButton(toolbar, text="Play / Resume", width=130, command=self.play_or_resume).pack(
            side="left", padx=6, pady=8)
        ctk.CTkButton(toolbar, text="Pause", width=90, command=self.pause_playback).pack(
            side="left", padx=6, pady=8)
        ctk.CTkButton(toolbar, text="Stop", width=90, command=self.stop_playback).pack(
            side="left", padx=6, pady=8)

        # --- Page counter: ◀  Page X / Y  ▶ ---
        # Placed right of the Stop button so navigation stays near playback controls.
        ctk.CTkButton(toolbar, text="◀", width=36,
                      command=lambda: self.skip_chunk(-1)).pack(side="left", padx=(10, 2), pady=8)
        self.page_counter_label = ctk.CTkLabel(
            toolbar, text="Page: 0 / 0",
            font=ctk.CTkFont(size=13, weight="bold"), width=110,
        )
        self.page_counter_label.pack(side="left", padx=2, pady=8)
        ctk.CTkButton(toolbar, text="▶", width=36,
                      command=lambda: self.skip_chunk(1)).pack(side="left", padx=(2, 10), pady=8)

        # --- File information labels ---
        info_frame = ctk.CTkFrame(self.root)
        info_frame.pack(fill="x", padx=14, pady=(0, 6))

        self.file_label = ctk.CTkLabel(info_frame, text="Current File: None")
        self.file_label.pack(side="left", padx=10, pady=6)
        self.page_label = ctk.CTkLabel(info_frame, text="Pages: 0")
        self.page_label.pack(side="left", padx=10, pady=6)

        # Go-to-page: a small entry field + button lets the user jump directly
        # to any page number after a PDF has been loaded.
        ctk.CTkLabel(info_frame, text="Go to page:").pack(side="left", padx=(16, 4), pady=6)
        self.goto_entry = ctk.CTkEntry(info_frame, width=60, placeholder_text="#")
        self.goto_entry.pack(side="left", padx=(0, 4), pady=6)
        # Pressing Enter inside the field or clicking Go both trigger the jump.
        self.goto_entry.bind("<Return>", lambda _e: self.jump_to_page())
        ctk.CTkButton(info_frame, text="Go", width=48, command=self.jump_to_page).pack(
            side="left", padx=(0, 10), pady=6)

        # --- Playback options: voice, rate, volume ---
        options_frame = ctk.CTkFrame(self.root)
        options_frame.pack(fill="x", padx=14, pady=(0, 6))

        # Voice dropdown – values are the edge-tts short-names.
        ctk.CTkLabel(options_frame, text="Voice:").pack(side="left", padx=(10, 4), pady=6)
        display_names = list(self.VOICES.values())
        self.voice_menu = ctk.CTkOptionMenu(
            options_frame, values=display_names,
            command=self._on_voice_selected, width=160,
        )
        self.voice_menu.set(display_names[0])
        self.voice_menu.pack(side="left", padx=(0, 12), pady=6)

        # Speech rate dropdown – edge-tts accepts strings like "+20%", "-10%".
        ctk.CTkLabel(options_frame, text="Rate:").pack(side="left", padx=(10, 4), pady=6)
        rate_options = ["-50%", "-25%", "+0%", "+25%", "+50%"]
        self.rate_menu = ctk.CTkOptionMenu(
            options_frame, values=rate_options,
            variable=self.rate_var, command=self._on_rate_selected, width=100,
        )
        self.rate_menu.set("+0%")
        self.rate_menu.pack(side="left", padx=(0, 12), pady=6)

        # Volume slider – controls pygame mixer volume (0.0 – 1.0).
        ctk.CTkLabel(options_frame, text="Volume:").pack(side="left", padx=(10, 4), pady=6)
        self.volume_slider = ctk.CTkSlider(
            options_frame, from_=0, to=1, variable=self.volume_var,
            command=self._on_volume_changed, width=140,
        )
        self.volume_slider.set(1.0)
        self.volume_slider.pack(side="left", padx=(0, 10), pady=6)

        # --- Progress section ---
        progress_frame = ctk.CTkFrame(self.root)
        progress_frame.pack(fill="x", padx=14, pady=(0, 6))

        self.elapsed_label = ctk.CTkLabel(progress_frame, text="Elapsed: 00:00")
        self.elapsed_label.pack(side="left", padx=10, pady=6)
        self.remaining_label = ctk.CTkLabel(progress_frame, text="Remaining: 00:00")
        self.remaining_label.pack(side="left", padx=10, pady=6)

        self.progress_bar = ctk.CTkProgressBar(progress_frame, width=300)
        self.progress_bar.set(0)
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=10, pady=6)

        # --- Scrollable text preview of the PDF content ---
        self.preview = ctk.CTkTextbox(self.root, wrap="word", font=ctk.CTkFont(size=13))
        self.preview.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        self.preview.insert("0.0", "Use the Load PDF button to choose a file.")
        self.preview.configure(state="disabled")  # read-only until a PDF is loaded

        # --- Status bar at the bottom ---
        self.status_label = ctk.CTkLabel(
            self.root, text="Load a PDF to begin.",
            text_color="#3b8ed0", font=ctk.CTkFont(size=12),
        )
        self.status_label.pack(anchor="w", padx=14, pady=(0, 10))

    # ──────────────────────────────────────────────────────────────
    # Voice / volume callbacks
    # ──────────────────────────────────────────────────────────────

    def _on_voice_selected(self, display_name: str) -> None:
        """Map the friendly display name back to the edge-tts voice id."""
        # Reverse-lookup: find the short-name whose display label matches.
        for short_name, label in self.VOICES.items():
            if label == display_name:
                self.voice_var.set(short_name)
                return

    def _on_rate_selected(self, new_rate: str) -> None:
        """Restart the current page with the newly selected speech rate.

        The rate value (e.g. '+25%') is stored in rate_var automatically by
        the CTkOptionMenu variable binding; here we re-synthesise the current
        page so the change is heard straight away instead of only on the next
        page turn.
        """
        self.rate_var.set(new_rate)  # ensure var is up to date
        if self.is_playing or self.is_paused:
            # Remember where we are, then stop and re-start from that page.
            page = self.current_index
            self._stop_engine()
            self.current_index = page
            self._update_position_label()
            self.play_or_resume()
        else:
            # Not currently playing; the new rate will be used on next Play.
            self.status_label.configure(text=f"Rate set to {new_rate}.")

    def _on_volume_changed(self, value: float) -> None:
        """Apply the new volume immediately if audio is currently playing."""
        pygame.mixer.music.set_volume(float(value))

    # ──────────────────────────────────────────────────────────────
    # PDF loading
    # ──────────────────────────────────────────────────────────────

    def load_pdf(self) -> None:
        """Open a file dialog and load the selected PDF for reading."""
        # Show the native OS file picker, filtered to PDF files only.
        file_path = filedialog.askopenfilename(
            title="Select a PDF file",
            filetypes=[("PDF Files", "*.pdf")],
        )
        if not file_path:
            return  # user cancelled

        # Stop any active playback before switching files.
        self._stop_engine()

        try:
            reader = PdfReader(file_path)

            # Extract text from every page; some pages may return None.
            pages_text = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(p.strip() for p in pages_text if p.strip())

            if not text:
                raise ValueError("The PDF does not contain readable text.")

            # Build a list of pages (one string per page) for reading.
            pages = self._extract_pages(reader)
            if not pages:
                raise ValueError("The PDF does not contain readable text.")

            # Store extracted data and reset playback position.
            self.pdf_path = file_path
            self.full_text = "\n\n".join(pages)
            self.text_chunks = pages
            self.current_index = 0

            # Update the info labels.
            self.file_label.configure(text=f"Current File: {os.path.basename(file_path)}")
            self.page_label.configure(text=f"Pages: {len(reader.pages)}")
            self._update_position_label()

            # Show the extracted text in the preview pane (read-only).
            self.preview.configure(state="normal")
            self.preview.delete("0.0", "end")
            self.preview.insert("0.0", self.full_text)
            self.preview.configure(state="disabled")

            self.progress_bar.set(0)
            self.status_label.configure(text="PDF loaded. Press Play to start reading.")

        except Exception as exc:
            messagebox.showerror("PDF Load Error", f"Unable to load the PDF.\n\n{exc}")
            self.status_label.configure(text="Loading failed.")

    # ──────────────────────────────────────────────────────────────
    # Page extraction
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_pages(reader: PdfReader) -> list[str]:
        """Return a list of non-empty page texts, one entry per PDF page.

        Whitespace within each page is normalised so the TTS engine receives
        clean, natural-sounding input.
        """
        pages = []
        for page in reader.pages:
            raw = page.extract_text() or ""
            # Collapse excessive whitespace while preserving sentence boundaries.
            clean = re.sub(r"[ \t]+", " ", raw).strip()
            if clean:
                pages.append(clean)
        return pages

    # ──────────────────────────────────────────────────────────────
    # TTS generation (edge-tts, async)
    # ──────────────────────────────────────────────────────────────

    def _generate_audio(self, text: str) -> bool:
        """Use edge-tts to synthesise *text* into TEMP_AUDIO (MP3).

        Runs the async edge_tts.Communicate in a fresh event loop so it can
        be called safely from a background thread.  Returns True on success.
        """
        voice = self.voice_var.get()
        rate = self.rate_var.get()

        async def _synth() -> None:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await communicate.save(TEMP_AUDIO)

        try:
            # Create a new event loop for this thread (threads don't have one).
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_synth())
            loop.close()
            return True
        except Exception as exc:
            # Schedule the error dialog on the main thread.
            self.root.after(0, lambda: self.status_label.configure(
                text=f"TTS error: {exc}"))
            return False

    # ──────────────────────────────────────────────────────────────
    # Playback controls
    # ──────────────────────────────────────────────────────────────

    def play_or_resume(self) -> None:
        """Start playback from the current chunk, or resume if paused."""
        if not self.text_chunks:
            messagebox.showwarning("No PDF Loaded", "Please load a PDF file first.")
            return

        # Resume from pause – pygame.mixer supports native unpause.
        if self.is_paused:
            pygame.mixer.music.unpause()
            self.is_paused = False
            self.is_playing = True
            # Record how far we were so elapsed-time maths stay correct.
            self.playback_start_time = time.time() - self.paused_offset
            self.status_label.configure(text="Playback resumed.")
            return

        # Guard against double-start.
        if self.is_playing:
            self.status_label.configure(text="Already playing.")
            return

        # Begin fresh playback in a background thread.
        self.stop_requested = False
        self.is_playing = True
        self.status_label.configure(text="Generating speech…")

        self.playback_thread = threading.Thread(target=self._playback_worker, daemon=True)
        self.playback_thread.start()

    def pause_playback(self) -> None:
        """Pause the active audio and remember the position for resume."""
        if not self.is_playing:
            self.status_label.configure(text="Nothing is playing.")
            return

        pygame.mixer.music.pause()
        self.is_paused = True
        self.is_playing = False
        # Store how many seconds have been played so far.
        self.paused_offset = time.time() - self.playback_start_time
        self.status_label.configure(text="Playback paused.")

    def stop_playback(self) -> None:
        """Stop playback completely and reset position."""
        self._stop_engine()
        self.status_label.configure(text="Stopped. You can load another PDF.")

    def skip_chunk(self, direction: int) -> None:
        """Jump forward (direction=+1) or backward (direction=-1) one chunk.

        Works whether playback is active, paused, or stopped.  When playing,
        the current audio is interrupted and the new chunk begins immediately.
        """
        if not self.text_chunks:
            self.status_label.configure(text="No PDF loaded.")
            return

        # Calculate the new index and clamp it within valid bounds.
        new_index = self.current_index + direction
        new_index = max(0, min(new_index, len(self.text_chunks) - 1))

        if new_index == self.current_index:
            # Already at the start or end of the document.
            self.status_label.configure(
                text="Already at the beginning." if direction < 0 else "Already at the last chunk.")
            return

        was_playing = self.is_playing
        was_paused = self.is_paused

        # Stop the current audio and wait for the thread so the new chunk
        # starts cleanly.  _stop_engine resets current_index to 0, so we
        # restore it to the desired position immediately afterward.
        self._stop_engine()
        self.current_index = new_index
        self._update_position_label()

        if was_playing or was_paused:
            # Restart playback from the new position.
            self.play_or_resume()
        else:
            self.status_label.configure(
                text=f"Moved to page {new_index + 1} / {len(self.text_chunks)}. Press Play to read.")

    def jump_to_page(self) -> None:
        """Jump directly to the page number typed in the Go-to-page entry field.

        Accepts 1-based page numbers matching what is displayed in the counter.
        Validates the input and shows an inline error if it is out of range.
        """
        if not self.text_chunks:
            self.status_label.configure(text="No PDF loaded.")
            return

        raw = self.goto_entry.get().strip()
        try:
            page_number = int(raw)
        except ValueError:
            self.status_label.configure(text=f"Invalid page number: '{raw}'")
            return

        total = len(self.text_chunks)
        if not (1 <= page_number <= total):
            self.status_label.configure(text=f"Page {page_number} is out of range (1 – {total}).")
            return

        # Convert from 1-based display number to 0-based internal index.
        target_index = page_number - 1
        if target_index == self.current_index and not self.is_playing and not self.is_paused:
            self.status_label.configure(text=f"Already at page {page_number}.")
            return

        was_playing = self.is_playing or self.is_paused
        self._stop_engine()
        self.current_index = target_index
        self._update_position_label()
        self.goto_entry.delete(0, "end")  # clear the field after a successful jump

        if was_playing:
            self.play_or_resume()
        else:
            self.status_label.configure(
                text=f"Jumped to page {page_number} / {total}. Press Play to read.")

    def _stop_engine(self) -> None:
        """Internal stop: halt audio, wait for the background thread to exit,
        then reset all playback state so the file can be replayed immediately.
        """
        self.stop_requested = True
        self.is_paused = False  # release any pause-spin so the thread sees the stop flag

        # Halt pygame audio immediately so the polling loop in the thread unblocks.
        try:
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()  # release the file handle for the next load
        except Exception:
            pass

        # Wait up to 5 seconds for the background thread to finish.
        # This prevents the old thread from interfering with the next playback session.
        if self.playback_thread and self.playback_thread.is_alive():
            self.playback_thread.join(timeout=5.0)
        self.playback_thread = None

        # Reset all playback state so Play works again without relaunching.
        self.is_playing = False
        self.current_index = 0
        self.paused_offset = 0.0
        self.audio_duration = 0.0
        self.progress_bar.set(0)
        self._update_position_label()
        self._update_time_labels(0, 0)
        self.stop_requested = False  # clear flag last, after thread has exited

    # ──────────────────────────────────────────────────────────────
    # Background playback worker
    # ──────────────────────────────────────────────────────────────

    def _playback_worker(self) -> None:
        """Background thread: iterate through text chunks, synthesise and play each.

        For every chunk the thread:
        1. Calls edge-tts to generate an MP3 file.
        2. Loads the MP3 into pygame and plays it.
        3. Polls pygame.mixer.music.get_busy() until the clip ends,
           updating the progress bar along the way.
        4. Advances to the next chunk (unless stop/pause was requested).
        """
        while self.current_index < len(self.text_chunks) and not self.stop_requested:
            chunk = self.text_chunks[self.current_index]

            # Update status on the main thread.
            idx = self.current_index
            self.root.after(0, lambda i=idx: self.status_label.configure(
                text=f"Generating speech for page {i + 1}/{len(self.text_chunks)}…"))

            # 1 – Synthesise the chunk to an MP3 file.
            if not self._generate_audio(chunk):
                break  # TTS failed; error already shown

            if self.stop_requested:
                break

            # 2 – Determine audio duration using mutagen.
            try:
                audio_info = MP3(TEMP_AUDIO)
                self.audio_duration = audio_info.info.length  # seconds
            except Exception:
                self.audio_duration = 5.0  # fallback estimate

            # 3 – Load and play the MP3 via pygame.
            pygame.mixer.music.load(TEMP_AUDIO)
            pygame.mixer.music.set_volume(float(self.volume_var.get()))
            pygame.mixer.music.play()
            self.playback_start_time = time.time()
            self.paused_offset = 0.0

            self.root.after(0, lambda i=idx: self.status_label.configure(
                text=f"Reading page {i + 1}/{len(self.text_chunks)}…"))
            self.root.after(0, self._update_position_label)

            # 4 – Poll until the clip finishes, updating progress each 200 ms.
            while pygame.mixer.music.get_busy() or self.is_paused:
                if self.stop_requested:
                    break
                if not self.is_paused:
                    elapsed = time.time() - self.playback_start_time
                    fraction = min(elapsed / max(self.audio_duration, 0.1), 1.0)
                    remaining = max(self.audio_duration - elapsed, 0)
                    self.root.after(0, lambda f=fraction: self.progress_bar.set(f))
                    self.root.after(0, lambda e=elapsed, r=remaining:
                                    self._update_time_labels(e, r))
                time.sleep(0.2)

            if self.stop_requested:
                break

            # Move to the next chunk.
            self.current_index += 1
            self.root.after(0, self._update_position_label)

        # Playback finished (naturally or via Stop).
        self.is_playing = False
        if not self.stop_requested and self.current_index >= len(self.text_chunks):
            self.root.after(0, lambda: self.status_label.configure(text="Playback complete."))
            self.root.after(0, lambda: self.progress_bar.set(1.0))

    # ──────────────────────────────────────────────────────────────
    # UI helpers
    # ──────────────────────────────────────────────────────────────

    def _update_position_label(self) -> None:
        """Refresh the page counter in the toolbar."""
        total = len(self.text_chunks)
        current = min(self.current_index + 1, total) if total else 0
        self.page_counter_label.configure(text=f"Page: {current} / {total}")

    def _update_time_labels(self, elapsed: float, remaining: float) -> None:
        """Set the elapsed / remaining labels from seconds values."""
        self.elapsed_label.configure(text=f"Elapsed: {self._fmt(elapsed)}")
        self.remaining_label.configure(text=f"Remaining: {self._fmt(remaining)}")

    @staticmethod
    def _fmt(seconds: float) -> str:
        """Format a duration in seconds as mm:ss."""
        s = max(0, int(seconds))
        m, s = divmod(s, 60)
        return f"{m:02d}:{s:02d}"


# ──────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────

def main() -> None:
    """Create the root window (TkinterDnD-enabled) and start the application."""
    # TkinterDnD.Tk() provides a standard Tk root with drag-and-drop support.
    # CustomTkinter widgets work normally on top of it.
    root = TkinterDnD.Tk()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    PDFReaderTTSApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
