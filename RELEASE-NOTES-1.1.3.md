# PAPro 1.1.3

## New
- **Community packs.** Pack Manager has a Community list of packs made by other users. Share your own from Pack Studio (Share with the community): the airline, code, character name and voice artist come from Voice details, and you only fill in what is empty and agree that the files are your own work. New packs are reviewed before they appear. Updates to a pack you already had approved go live straight away.
- **Sample.** Community packs have a Sample button that plays 10 seconds of the pack's After Landing PA, built with its first airport and a local time of 3:45 PM.
- **Ratings.** Rate community packs out of 5 stars; the average shows beside the pack name.
- **After Landing parts.** New packs build After Landing from Part 1, Airport, Part 2, Time and Part 3, and more parts can be added.
- **Pack Studio buttons.** Delete (with a red confirm), Open project folder, Upload to Community, Save, Export.
- **Author key.** Settings, Advanced: copy your author key as a backup and restore it on a new PC, or use a transfer code from the reviewer to move a pack to a new PC.
- **Pack Manager.** Your installed packs and the Community list are on one tab, both collapsible, with a search box. The tab now comes before Pack Studio.
- **PAPro's own window.** No more Edge window: PAPro opens in its own window with its own taskbar icon and remembers where you left it. (Needs Microsoft WebView2, built into Windows 11; otherwise the browser window is used.)
- **Skip PA and Replay PA** under the volume knob. Opening PAPro part way through a flight no longer plays a PA: the one for your phase is ready on Replay.

## Aircraft other than the Fenix
- **Audio.** PAs play cleanly at your output device's own sample rate. One sound at a time: boarding music pauses during a PA and resumes where it left off. The boarding music track is random each time PAPro loads. PAPro shows as "PAPro" in the Windows volume mixer.
- **Flight phase.** On opening, PAPro looks at the sim for a few seconds, then shows the right phase (in the air, taxi in, deboarding).
  - Cabin service plays on reaching cruise altitude.
  - After Landing plays once you are off the runway (at most 90 s after touchdown).
  - Disarm doors plays 5 s after all engines are off.
  - The deboarding PA waits for GSX to start deboarding.
  - The phase stays on Deboarding until a different flight plan is loaded, and the welcome never plays after a flight.
- **GSX.** Boarding completed or aborted in GSX triggers Boarding Complete. Manual boarding has a Boarding Complete button. GSX is read automatically; the setting is gone.
- **PMDG 737.** The seat belt sign is read through the PMDG SDK. The seat belt off PA plays only when it can be read (not yet verified in the sim).

## Settings
- Removed: play through PAPro, how the pieces join (Natural flow is used) and the GSX switch (always on).
- Web address, Fenix packs folder and log are under Advanced.
