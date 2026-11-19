# Task: look at the finished video's picture (S5v)

You check the picture of one finished video before a person sees it. The pipeline built it from
the transcript; code has already measured what it can (frame size, black bars, black or frozen
picture, silence, loudness). You answer what code cannot see.

`input.clip` says what kind of video it is (vertical reel or horizontal video) and whether the
subtitles are burned into the picture.

`input.attachments` gives the absolute path of ONE image: a contact sheet of frames taken evenly
across the video, in order left to right, top to bottom, numbered 1..N in that order (the numbers
are not drawn on the image). Open the image file and look at every frame. If you cannot open it,
do not guess: write nothing and report that the image could not be opened.

Answer these questions about the frames as a whole:

1. `face_in_frame` — is the speaker's face fully inside the frame in the frames where a person
   is speaking to the camera? `yes`, `no` (the face is cut by the frame edge, or the person is
   pushed off-frame while an empty background is centred), or `no_face` (no speaker visible at
   all, e.g. only a screen share).
2. `personal_data` — is anything personal readable on screen: messenger chats, e-mail or contact
   lists, phone numbers, addresses, documents, payment or account details, notifications with
   names? `yes` or `no`. Names on a slide that the speaker presents on purpose are not personal
   data; a private chat window in the background is.
3. `subtitles_readable` — are the burned-in subtitles readable (enough contrast, not cut off at
   the edges, not too small)? `yes`, `no`, or `no_subtitles` when the video has none.
4. `subtitles_cover_face` — do the subtitles or the channel plate cover the speaker's mouth or
   eyes? `yes` or `no` (`no` also when there are no subtitles).
5. `bars_or_empty` — black bars, a blurred or empty band, or a picture inside a picture that
   leaves part of the frame empty? `yes` or `no`.

Answer with ONLY this YAML, no code fences, no text before or after:

face_in_frame: yes
personal_data: no
subtitles_readable: yes
subtitles_cover_face: no
bars_or_empty: no
problem_frames: []
note: one short sentence in Russian about what you saw

`problem_frames` lists the numbers of frames that show a problem from questions 1–5 (empty when
there is none). Never write times, timecodes, file names or links.
