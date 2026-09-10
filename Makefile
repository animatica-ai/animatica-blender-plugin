# Animatica for Blender — package the addon as an installable .zip.
#
# `make zip` produces dist/animatica-blender-<version>.zip with a single
# top-level `animatica_blender/` directory inside, which is exactly what
# Blender's `Install Addon…` UI expects.
#
# `make install` symlinks the source tree into your Blender 5+ addons
# directory so editing files lands live in Blender on the next reload.
# Override BLENDER_ADDONS_DIR if your install path is non-standard.

ADDON         := animatica_blender
VERSION       := $(shell python3 -c "import re,pathlib;t=pathlib.Path('$(ADDON)/__init__.py').read_text();m=re.search(r'\"version\":\s*\(([\d, ]+)\)',t);print('.'.join(p.strip() for p in m.group(1).split(',')))")
DIST          := dist
ZIP           := $(DIST)/animatica-blender-$(VERSION).zip

# macOS default Blender 5.x addon path. Override on Linux/Windows.
BLENDER_ADDONS_DIR ?= $(HOME)/Library/Application Support/Blender/5.0/scripts/addons

.PHONY: zip clean install uninstall info

zip: $(ZIP)

$(ZIP): $(shell find $(ADDON) -name '*.py' -not -path '*/__pycache__/*')
	@mkdir -p $(DIST)
	@find $(ADDON) -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	zip -r $(ZIP) $(ADDON) -x '*/__pycache__/*' -x '*.pyc'
	@echo "→ $(ZIP)"

install:
	@mkdir -p "$(BLENDER_ADDONS_DIR)"
	@if [ -e "$(BLENDER_ADDONS_DIR)/$(ADDON)" ]; then \
		echo "Removing existing $(BLENDER_ADDONS_DIR)/$(ADDON)"; \
		rm -rf "$(BLENDER_ADDONS_DIR)/$(ADDON)"; \
	fi
	ln -s "$(CURDIR)/$(ADDON)" "$(BLENDER_ADDONS_DIR)/$(ADDON)"
	@echo "→ symlinked to $(BLENDER_ADDONS_DIR)/$(ADDON)"
	@echo "  enable in Blender: Edit > Preferences > Add-ons > 'Animatica'"

uninstall:
	@if [ -L "$(BLENDER_ADDONS_DIR)/$(ADDON)" ]; then \
		rm "$(BLENDER_ADDONS_DIR)/$(ADDON)"; \
		echo "→ removed symlink $(BLENDER_ADDONS_DIR)/$(ADDON)"; \
	else \
		echo "no symlink at $(BLENDER_ADDONS_DIR)/$(ADDON)"; \
	fi

clean:
	rm -rf $(DIST)
	find $(ADDON) -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

info:
	@echo "addon:     $(ADDON)"
	@echo "version:   $(VERSION)"
	@echo "zip:       $(ZIP)"
	@echo "install:   $(BLENDER_ADDONS_DIR)/$(ADDON)"
