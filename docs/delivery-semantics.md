# Delivery semantics

GreyQueue provides at-least-once execution with bounded retries under eventual recovery of PostgreSQL, coordinators and workers. An accepted job can end in success, permanent failure, cancellation or dead letter. There is no unconditional eventual-success guarantee.

A claim is delivery; the task body is execution; an independent sink commit is effect completion; the HTTP finish response is acknowledgement; the PostgreSQL completion transaction is result persistence. These are different moments. A crash between a sink commit and result persistence creates an ambiguous outcome. Retrying may duplicate the effect.

An increasing fence prevents an older attempt from changing GreyQueue state after reassignment. External systems need their own idempotency key or fence enforcement. A sink that cannot participate in either cannot gain exactly-once effects merely because GreyQueue has leases. Submission idempotency only prevents duplicate job creation.

`python -m scripts.side_effects` demonstrates two executions producing two effects, then two executions producing one effect when the independent sink records an operation key transactionally.
