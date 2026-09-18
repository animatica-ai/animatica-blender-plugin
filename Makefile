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
# Tack a label onto the zip's name without touching bl_info, which only takes
# numbers: `make zip VERSION_SUFFIX=-dev` builds animatica-blender-X.Y.Z-dev.zip
# for a build that is not a release.
VERSION_SUFFIX ?=
ZIP           := $(DIST)/animatica-blender-$(VERSION)$(VERSION_SUFFIX).zip
# What this build calls itself to the updater: v0.6.0, or v0.6.0-preview2.
VERSION_TAG   := v$(VERSION)$(VERSION_SUFFIX)
STAMP_TAG      = python3 -c 'import re,sys,pathlib;p=pathlib.Path(sys.argv[1]);\
p.write_text(re.sub(r"^VERSION_TAG = .*$$", "VERSION_TAG = \"$(VERSION_TAG)\"", \
p.read_text(), count=1, flags=re.M))' 

# macOS default Blender 5.x addon path. Override on Linux/Windows.
BLENDER_ADDONS_DIR ?= $(HOME)/Library/Application Support/Blender/5.0/scripts/addons

.PHONY: zip clean install uninstall info

zip: $(ZIP)

# A build that carries the weights, for handing to someone who should not have
# to set anything up: MODEL_DIR is a bundle directory (poser.onnx, ik.onnx,
# meta.json). Staged in a temp tree rather than copied into the source, so
# 140 MB never lands in the working copy — or in git.
#
#   make zip-with-model MODEL_DIR="~/Library/.../animatica_autoposer/current" VERSION_SUFFIX=-dev
zip-with-model:
	@test -n "$(MODEL_DIR)" || { echo "set MODEL_DIR=<bundle directory>"; exit 1; }
	@test -f "$(MODEL_DIR)/meta.json" || { echo "no meta.json in $(MODEL_DIR)"; exit 1; }
	@mkdir -p $(DIST)
	@rm -rf $(DIST)/.stage && mkdir -p $(DIST)/.stage
	@find $(ADDON) -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	@cp -R $(ADDON) $(DIST)/.stage/$(ADDON)
	@$(STAMP_TAG) $(DIST)/.stage/$(ADDON)/__init__.py
	@mkdir -p $(DIST)/.stage/$(ADDON)/autoposer/model
	@cp "$(MODEL_DIR)"/poser.onnx "$(MODEL_DIR)"/ik.onnx "$(MODEL_DIR)"/meta.json 		$(DIST)/.stage/$(ADDON)/autoposer/model/
	@rm -f $(ZIP)
	@cd $(DIST)/.stage && zip -qr ../$(notdir $(ZIP)) $(ADDON) -x '*/__pycache__/*' -x '*.pyc'
	@rm -rf $(DIST)/.stage
	@echo "→ $(ZIP) (with model)"

# Staged rather than zipped in place, so the build can stamp its own release
# tag into the copy without touching the working tree. The updater compares
# that tag, which is the only way preview2 can know it is newer than preview1.
$(ZIP): $(shell find $(ADDON) -name '*.py' -not -path '*/__pycache__/*')
	@mkdir -p $(DIST)
	@rm -rf $(DIST)/.stage && mkdir -p $(DIST)/.stage
	@find $(ADDON) -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	@cp -R $(ADDON) $(DIST)/.stage/$(ADDON)
	@$(STAMP_TAG) $(DIST)/.stage/$(ADDON)/__init__.py
	@rm -f $(ZIP)
	@cd $(DIST)/.stage && zip -qr ../$(notdir $(ZIP)) $(ADDON) -x '*/__pycache__/*' -x '*.pyc'
	@rm -rf $(DIST)/.stage
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
