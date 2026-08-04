# sashiko-drafts

Generate plaintext email drafts replying to [sashiko](https://sashiko.dev) &
[netdev-sashiko](https://netdev-ai.bots.linux.dev/sashiko/) reviews.

**Setup:**

 - Manage your series using `b4`.
 - Check your Git `sendemail.*` credentials for IMAP draft upload.
 - Put your signature in Git `format.signature` or `format.signatureFile`.

**Usage:**

```
⟩ uvx sashiko-drafts
found b4 msgid:  20260731-macb-context-v6-0-49d5a1439d48%40bootlin.com

non-net sashiko: https://sashiko.dev/#/patchset/20260731-macb-context-v6-0-49d5a1439d48%40bootlin.com
net sashiko:     https://netdev-ai.bots.linux.dev/sashiko/#/patchset/20260731-macb-context-v6-0-49d5a1439d48%40bootlin.com

[PATCH net-next v6 01/16] net: macb: drop "consistent" from alloc/free function names (skipped)
[PATCH net-next v6 02/16] net: macb: unify device pointer naming convention (skipped)
[PATCH net-next v6 03/16] net: macb: unify variable naming convention in at91ether functions (skipped)
[PATCH net-next v6 04/16] net: macb: unify queue index variable naming convention and types (skipped)
[PATCH net-next v6 05/16] net: macb: enforce reverse christmas tree (RCT) convention (skipped)
[PATCH net-next v6 06/16] net: macb: allocate tieoff descriptor once across device lifetime (skipped)
[PATCH net-next v6 07/16] net: macb: introduce macb_context struct for buffer management (2 new findings)
[PATCH net-next v6 08/16] net: macb: avoid macb_init_rx_buffer_size() modifying state (skipped)
[PATCH net-next v6 09/16] net: macb: make `struct macb` subset reachable from macb_context struct (skipped)
[PATCH net-next v6 10/16] net: macb: change caps helpers signatures (skipped)
[PATCH net-next v6 11/16] net: macb: change function signatures to take contexts (1 new finding)
[PATCH net-next v6 12/16] net: macb: introduce macb_context_alloc() helper (skipped)
[PATCH net-next v6 13/16] net: macb: move printk() calls out of bp->lock critical section (1 new finding)
[PATCH net-next v6 14/16] net: macb: read ISR inside bp->lock critical section (5 new findings)
[PATCH net-next v6 15/16] net: macb: use context swapping in .set_ringparam() (10 new findings)
[PATCH net-next v6 16/16] net: macb: use context swapping in .ndo_change_mtu() (8 new findings)

wrote 6 drafts to IMAP Drafts
```

**Features:**

 - Use `--msgid` to pick any series. It supports Message-IDs or [Lore](https://lore.kernel.org/) links.
 - Use `--output-dir` to generate files on disk rather than IMAP upload.
 - Use `--signature` / `--no-signature` / `--signature-file` to override the signature lookup behavior.
 - It ignores patches that only contain pre-existing findings.

**Example usage:**

Replies on [this series
](https://lore.kernel.org/netdev/20260731-macb-context-v6-0-49d5a1439d48@bootlin.com/#r)
were done using drafts generated with this script.

**Install:**

```
uv tool install sashiko-drafts
# or
pip install sashiko-drafts
```
