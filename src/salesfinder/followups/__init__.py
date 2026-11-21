"""Follow-ups: when a sequence's step 2 falls due, it is gated and queued in the Outbox for a
teammate to approve (``service.queue_due``). Nothing here sends."""
