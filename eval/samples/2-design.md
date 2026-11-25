Yes, a cache in front of the image service fits here. The service answers in about 420 ms, and most requests ask for the same 200 thumbnails.

The guide for the cache library warns that entries "may be served after they expire, for up to one refresh interval". So the design checks the age of an entry instead of trusting the library.

## Design

1. **Two tiers.** Memory first, then disk. The memory tier holds 256 entries, and the disk tier holds up to 4 GB.
2. **Keys.** A key is the SHA-256 of the image URL plus its width and height. The service already sends an `ETag`, and the cache stores it with the entry.
   - A request with a matching `ETag` gets a `304` and no body.
   - A changed image gets a new key, so no old copy is ever served for it.
3. **Expiry.** Each entry lives 15 minutes. A background job removes expired entries every 60 seconds.
   - The job runs at low priority and stops after 50 ms of work per run.
   - The config flag is `"strict_expiry": true`, which the library added in version 3.2.
4. **Limits.**
   - The cache never stores a response larger than 8 MB.
   - It never stores a response with `Cache-Control: private`.
5. **Metrics.** The cache counts hits, misses and evictions, and it reports them on `/metrics` every 30 seconds. A hit rate below 70% for one hour raises an alert.
6. **Rollout.** First 5% of traffic for two days, then 50%, then all of it. A flag in `config/cache.yaml` turns the cache off at once.
