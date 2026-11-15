title: Add GarageBooks to projects.md
Adds [GarageBooks](https://github.com/mattew124/garagebooks) to the projects page. It was proposed as teslamate-expenses in #5965; this addresses the points raised there:

- **Name:** renamed to GarageBooks. The TeslaMate name is no longer in its name, repository, image or package identifiers.
- **Disclaimer:** the text from TRADEMARK.md section 6 appears word for word in its README, its documentation and this entry: "This project is an unofficial community tool and is not affiliated with, endorsed by, or supported by the official TeslaMate project."
- **Licence:** MIT, with no TeslaMate code or schema in the repository. Its tests create TeslaMate's tables by running the published TeslaMate image.
- **Database:** the entry says plainly that it reads TeslaMate's tables directly and is not MQTT-based, as the page's introduction describes for listed projects. It never writes to TeslaMate's tables (its own data is in a separate `expenses` schema), and it refuses to start on a TeslaMate release newer than the one it was tested with, so a schema change can't produce wrong figures.
- **CLA:** signed.
