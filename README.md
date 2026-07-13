# comma connect

The web and mobile companion application for [openpilot](https://github.com/commaai/openpilot)

Try it with your openpilot device:
- **stable:** https://connect.comma.ai
- **latest:** https://latest.connect-d5y.pages.dev/

## Self-hosted fork

This fork runs an independent, self-hosted instance on AWS (S3 + CloudFront frontend, plus a Lambda
that mimics comma's upload API so a comma device can store drives in a self-owned S3 bucket). See
[`CLAUDE.md`](./CLAUDE.md) for the full deployment: infrastructure inventory, frontend redeploy
steps, the upload pipeline and device patches, and how upload/IP logging is checked.

- **Live URL:** https://comma-connect.aws.internetchen.de
- **Client IP logging:** the upload Lambda records both the device's public WAN IP (`src_ip`) and
  its internal LAN IP (`local_ip`) to CloudWatch on every upload request — see the "Client IP
  logging" section in `CLAUDE.md` for the query commands.

## Development
* Install pnpm: https://pnpm.io/installation
* Install dependencies: `pnpm install`
* Start dev server: `pnpm start`

## Contributing

If you don't have a comma device, connect has a demo mode with some example drives. This should allow for testing most functionality except for interactions with the device, such as getting the car battery voltage.

* Use best practices
* Write test cases
* Keep files small and clean
* Use branches / pull requests to isolate work. Don't do work that can't be merged quickly, find ways to break it up

## Libraries Used
There's a ton of them, but these are worth mentioning because they sort of affect everything.

 * `React` - Object oriented components with basic lifecycle callbacks rendered by state and prop changes.
 * `Redux` - Sane formal *global* scope. This is not a replacement for component state, which is the best way to store local component level variables and trigger re-renders. Redux state is for global state that many unrelated components care about. No free-form editing, only specific pre-defined actions. [Redux DevTools](https://chrome.google.com/webstore/detail/redux-devtools/lmhkpmbekcpmknklioeibfkpmmfibljd?hl=en) can be very helpful.
 * `@material-ui` - Lots of fully featured highly customizable components for building the UIs with. Theming system with global and per-component overrides of any CSS values.
 * `react-router-redux` - the newer one, 5.x.... Mindlessly simple routing with convenient global access due to redux
