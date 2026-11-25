# PAPro 1.1.3

- **Pack Manager.** Installed packs and the Community list are in one tab, both collapsible, with a search box. The tab now comes before Pack Studio.
- **Community packs.** Share a pack from Pack Studio: the airline, voice artist and character name come from Voice details (asked for only if empty), and you agree the files are your own work. Packs are reviewed before they appear; updates to your own approved packs go live straight away. Rate packs out of 5. Authors can back up or restore their author key under Settings, Advanced. The service is in `community/`.
- **PAPro's own window.** No more Edge window: PAPro opens in its own window with its own taskbar icon, and remembers where you left it. (Needs Microsoft WebView2, which Windows 11 has; otherwise the browser window is used.)
- **Audio.** PAs play cleanly at your output device's own sample rate. One sound at a time: boarding music pauses during a PA and resumes where it left off. PAPro shows as "PAPro" in the Windows volume mixer. The boarding music track is random each time PAPro loads.
- **Skip PA / Replay PA** under the volume knob. Opening PAPro part way through a flight no longer plays a PA: the one for your phase is ready on Replay.
- **Flight phase.** PAPro looks at the sim for a few seconds on opening, then shows the right phase (air, taxi in, deboarding). Cabin service plays on reaching cruise altitude. After Landing plays once you are off the runway (at most 90 s after touchdown). Disarm doors plays 5 s after all engines are off. The deboarding PA waits for GSX. The phase stays on Deboarding until a different flight plan is loaded, and the welcome never plays after a flight.
- **GSX.** Boarding completed or aborted in GSX triggers Boarding Complete. Manual boarding has a Boarding Complete button.
- **PMDG 737.** The seat belt sign is read through the PMDG SDK; the seat belt off PA plays only when it can be read (unverified in the sim).
- **Settings.** Removed: play through PAPro, how the pieces join (Natural flow) and the GSX switch (on). Web address, Fenix packs folder and log are under Advanced.
- Installed airline packs can be collapsed.
