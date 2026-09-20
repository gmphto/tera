"""The local HTTP service the desktop client drives (issue #27).

`backend.api.service` holds the transport, the closed route table and the
command; `backend.api.library` and `backend.api.imports` hold the operations;
`backend.api.schemas` holds request validation; `backend.api.errors` holds the
closed code table and the one error envelope.

The library, import, error and schema modules import no `backend.intelligence.*`
module and read no credential; `backend.api.recommendations` is the single module
that may import `backend.intelligence.jev` and `backend.intelligence.questions`,
and no module here opens an outbound socket of its own. No response carries an
audio byte, a filesystem path or a file. The library, the queue and their
vocabulary belong to #21, #22 and #23; this package only serves them.
"""
