# frontend

The simulated internet banking client (component C1), a static site of two screens: login and
fund transfer. Plain HTML and vanilla JavaScript with no framework and no build toolchain, so it
can be uploaded to Amazon S3 as is and served through CloudFront.

The capture layer records keydown and keyup timestamps only. Key identity is discarded in the
browser and typed content is never transmitted, which satisfies objective O1. Timing vectors are
batched per field and posted over HTTPS to the API Gateway endpoint.

Files expected here: `index.html` and `transfer.html` in `src/`, the capture and submission
JavaScript, and a small stylesheet.

Owner: Aditya Shukla (23BIT0250).
