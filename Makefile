.PHONY: css css-build css-install

# Install Node dependencies (tailwindcss)
css-install:
	npm install

# Watch mode for development
css: css-install
	npx tailwindcss -i dango/web/static/css/input.css -o dango/web/static/css/tailwind.min.css --watch

# Production build (minified, commit the output)
css-build: css-install
	npx tailwindcss -i dango/web/static/css/input.css -o dango/web/static/css/tailwind.min.css --minify
