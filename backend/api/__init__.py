"""The local HTTP service the desktop client drives (issue #27).

`backend.api.service` holds the transport, the closed route table and the
command; `backend.api.library` and `backend.api.imports` hold the operations;
`backend.api.schemas` holds request validation; `backend.api.errors` holds the
closed code table and the one error envelope.

Nothing in this package imports `backend.intelligence.*`, reads a credential,
opens an outbound socket or returns an audio byte, a filesystem path or a file.
The library, the queue and their vocabulary belong to #21, #22 and #23; this
package only serves them.
"""
