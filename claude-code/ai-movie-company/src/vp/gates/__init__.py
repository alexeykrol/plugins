"""In-dialog gate commands (UX v1): `review ...` (S3b) and `approve ...` (S9).

They write the SAME files as the file path (fragments.review.yaml, approval.yaml) and validate with
the SAME code (`s3b_review.check_review`, `_package` helpers), so a gate can be passed in a chat,
in Telegram or by editing the file, and continued from any mode. A model never edits these files
by hand: every change goes through these commands, which refuse invalid input without writing.
"""
