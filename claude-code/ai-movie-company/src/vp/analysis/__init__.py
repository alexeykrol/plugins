"""Material analysis without speech (SCENARIOS capabilities К, Б, М, Ч).

  scenes.py    shots of a video / a folder of videos: cuts, keyframes, simple measures (К)
  beats.py     tempo, beats and bars of a music file (Б)
  montage.py   edit plan for a music montage from shots + beats, and its render (М, Б)
  takes.py     cut list for raw talking-head takes: pauses, fillers, restarts (Ч)
  render_plan  the shared ffmpeg renderer of a segment plan

Judgment (what a keyframe shows, whether a cut is right) is the model's / the person's and goes
into the files through the CLI; everything counted (times, frames, beats) is code. Models never see
or write times. No dependencies beyond ffmpeg and the standard library.
"""
