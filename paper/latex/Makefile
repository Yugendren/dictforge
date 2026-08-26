# Makefile for the dictforge DCC submission (paper/latex/).
#
#   make          -- generate tables, then pdflatex + bibtex + pdflatex x2
#   make tables   -- (re)generate paper/latex/tables/*.tex from
#                    results/benchmark_v2.csv (+ results/parity_v2.csv if
#                    present) via ../../tools/make_tables.py
#   make clean    -- remove LaTeX build byproducts (keeps main.pdf and
#                    the generated tables/*.tex)
#   make distclean-- clean, plus remove main.pdf and generated tables

PYTHON     ?= python3
MAKE_TABLES = ../../tools/make_tables.py
TABLES_DIR  = tables
TABLE_FILES = $(TABLES_DIR)/t1_main.tex $(TABLES_DIR)/t2_equalcompute.tex \
              $(TABLES_DIR)/t3_baselines.tex $(TABLES_DIR)/t4_parity.tex

MAIN        = main
BIB         = references

.PHONY: all tables clean distclean

all: $(MAIN).pdf

tables: $(TABLE_FILES)

# Regenerate whenever invoked explicitly; also declared PHONY-ish via the
# .tex targets depending on the CSV inputs so `make` picks up changes.
$(TABLE_FILES): $(MAKE_TABLES) ../../results/benchmark_v2.csv
	$(PYTHON) $(MAKE_TABLES)

$(MAIN).pdf: $(MAIN).tex $(BIB).bib $(TABLE_FILES)
	pdflatex -interaction=nonstopmode -halt-on-error $(MAIN).tex
	bibtex $(MAIN)
	pdflatex -interaction=nonstopmode -halt-on-error $(MAIN).tex
	pdflatex -interaction=nonstopmode -halt-on-error $(MAIN).tex

clean:
	rm -f $(MAIN).aux $(MAIN).bbl $(MAIN).blg $(MAIN).log $(MAIN).out \
	      $(MAIN).toc $(MAIN).lof $(MAIN).lot $(MAIN).fls \
	      $(MAIN).fdb_latexmk $(MAIN).synctex.gz

distclean: clean
	rm -f $(MAIN).pdf $(TABLE_FILES)
