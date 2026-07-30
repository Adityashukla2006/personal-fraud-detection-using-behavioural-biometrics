# frontend/src

Source files for the static banking client.

Files expected here:

- `index.html`, the login screen.
- `transfer.html`, the fund transfer screen.
- `capture.js`, the keystroke capture layer: keydown and keyup timestamp collection, key identity
  discarded at capture time, per field batching.
- `api.js`, the HTTPS submission of timing vectors to the API Gateway endpoint and handling of the
  returned risk tier.
- `styles.css`.

No build step, no bundler and no package manager. The files are uploaded to S3 unchanged.

Owner: Aditya Shukla (23BIT0250).
