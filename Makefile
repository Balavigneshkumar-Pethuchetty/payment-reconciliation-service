PROJECT   = payment-reconciliation-service

# Detect docker compose v2 (plugin) vs legacy docker-compose
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

# ── Colour codes ───────────────────────────────────────────────────────────────
BOLD  = \033[1m
RESET = \033[0m
GRN   = \033[0;32m
CYN   = \033[0;36m
YLW   = \033[0;33m
RED   = \033[0;31m
DIM   = \033[2m
BLU   = \033[0;34m
MGN   = \033[0;35m

.PHONY: up build rebuild down restart logs logs-api logs-db logs-ollama logs-ollama-chat logs-pgadmin \
        ps status urls open shell-api shell-db \
        ollama-pull ollama-list migrate clean restart-tunnel help

.DEFAULT_GOAL := help

# ── Start (no rebuild) ────────────────────────────────────────────────────────
up: .env
	@echo "$(BOLD)$(GRN)▶  Starting $(PROJECT)…$(RESET)"
	@$(COMPOSE) up -d
	@$(MAKE) --no-print-directory _wait
	@$(MAKE) --no-print-directory urls

# ── Build images then start (after code changes) ──────────────────────────────
build: .env
	@echo "$(BOLD)$(GRN)▶  Building and starting $(PROJECT)…$(RESET)"
	@$(COMPOSE) up --build -d
	@$(MAKE) --no-print-directory _wait
	@$(MAKE) --no-print-directory urls

# ── Force-rebuild from scratch (clears layer cache) ───────────────────────────
rebuild: .env
	@echo "$(BOLD)$(YLW)▶  Force-rebuilding $(PROJECT) (no cache)…$(RESET)"
	@$(COMPOSE) build --no-cache
	@$(COMPOSE) up -d
	@$(MAKE) --no-print-directory _wait
	@$(MAKE) --no-print-directory urls

# ── Stop containers, keep volumes ─────────────────────────────────────────────
down:
	@echo "$(BOLD)$(YLW)▶  Stopping $(PROJECT)…$(RESET)"
	@$(COMPOSE) down
	@echo "$(DIM)  Volumes preserved. Run 'make clean' to wipe them.$(RESET)"

# ── Restart all containers ────────────────────────────────────────────────────
restart:
	@echo "$(BOLD)$(YLW)▶  Restarting $(PROJECT)…$(RESET)"
	@$(COMPOSE) restart
	@$(MAKE) --no-print-directory _wait
	@$(MAKE) --no-print-directory urls

# ── Logs ──────────────────────────────────────────────────────────────────────
logs:
	@$(COMPOSE) logs -f

logs-api:
	@$(COMPOSE) logs -f api

logs-db:
	@$(COMPOSE) logs -f db

logs-ollama:
	@$(COMPOSE) logs -f ollama

logs-ollama-chat:
	@$(COMPOSE) logs -f ollama-chat

logs-pgadmin:
	@$(COMPOSE) logs -f pgadmin

# ── Container status ──────────────────────────────────────────────────────────
ps:
	@echo "$(BOLD)$(CYN)  Container status$(RESET)"
	@$(COMPOSE) ps

status: ps

# ── Interactive shells ────────────────────────────────────────────────────────
shell-api:
	@$(COMPOSE) exec api /bin/bash

shell-db:
	@$(COMPOSE) exec db psql -U postgres -d payment_reconciliation

# ── Ollama — runs as Docker container (port 11434) ───────────────────────────
ollama-pull:
	@echo "$(BOLD)$(BLU)▶  Pulling llava into containerised Ollama…$(RESET)"
	@$(COMPOSE) exec ollama ollama pull llava
	@echo "$(GRN)  Model ready.$(RESET)"

ollama-list:
	@echo "$(BOLD)$(BLU)  Models available in Ollama container:$(RESET)"
	@$(COMPOSE) exec ollama ollama list

# ── Apply DB trigger from migrations/init.sql (run once after first up) ───────
migrate:
	@echo "$(BOLD)$(BLU)▶  Applying DB migrations…$(RESET)"
	@$(COMPOSE) exec db psql -U postgres -d payment_reconciliation \
	    -f /dev/stdin < migrations/init.sql
	@echo "$(GRN)  Done.$(RESET)"

# ── Print all service URLs ────────────────────────────────────────────────────
urls:
	@echo ""
	@echo "$(BOLD)$(CYN)╔══════════════════════════════════════════════════════════════════╗$(RESET)"
	@echo "$(BOLD)$(CYN)║          Payment Reconciliation Service — All URLs               ║$(RESET)"
	@echo "$(BOLD)$(CYN)╚══════════════════════════════════════════════════════════════════╝$(RESET)"
	@echo ""
	@echo "  $(BOLD)$(MGN)── Production (Cloudflare Tunnel) ───────────────────────────────────$(RESET)"
	@echo "    $(MGN)https://pay.gm-global-techies-town.club$(RESET)              API health"
	@echo "    $(MGN)https://pay.gm-global-techies-town.club/docs$(RESET)         Swagger UI"
	@echo "    $(MGN)https://pay.gm-global-techies-town.club/redoc$(RESET)        ReDoc"
	@echo "    $(MGN)https://pay.gm-global-techies-town.club/events/subscribe$(RESET)  SSE stream"
	@echo "    $(MGN)https://chat.gm-global-techies-town.club$(RESET)             Ollama Chat"
	@echo ""
	@echo "  $(BOLD)$(GRN)── Local HTTP Services ──────────────────────────────────────────────$(RESET)"
	@echo "    $(GRN)http://localhost:8001$(RESET)        $(BOLD)API$(RESET)         health / Swagger at /docs"
	@echo "    $(GRN)http://localhost:8082$(RESET)        $(BOLD)Ollama Chat$(RESET) https://chat.gm-global-techies-town.club"
	@echo "    $(GRN)http://localhost:5050$(RESET)        $(BOLD)pgAdmin$(RESET)     DB browser"
	@echo "    $(GRN)http://localhost:11434$(RESET)       $(BOLD)Ollama$(RESET)      REST API"
	@echo "    $(GRN)http://localhost:11434/api/tags$(RESET)  Ollama model list"
	@echo ""
	@echo "  $(BOLD)$(CYN)── pgAdmin ───────────────────────────────────────────────────────────$(RESET)"
	@echo "    $(BOLD)URL      :$(RESET) $(GRN)http://localhost:5050$(RESET)"
	@echo "    $(BOLD)Login    :$(RESET) admin@example.com  /  admin123"
	@echo "    $(BOLD)― Add Server connection ―$(RESET)"
	@echo "    $(BOLD)  Host     :$(RESET) db"
	@echo "    $(BOLD)  Port     :$(RESET) 5432"
	@echo "    $(BOLD)  Database :$(RESET) payment_reconciliation"
	@echo "    $(BOLD)  Username :$(RESET) postgres"
	@echo "    $(BOLD)  Password :$(RESET) password"
	@echo "    $(DIM)  run 'make shell-db' for a psql prompt instead$(RESET)"
	@echo ""
	@echo "  $(BOLD)$(CYN)── Keycloak (external) ──────────────────────────────────────────────$(RESET)"
	@echo "    $(MGN)https://auth.gm-global-techies-town.club$(RESET)        login"
	@echo "    $(MGN)https://auth.gm-global-techies-town.club/admin$(RESET)  admin console"
	@echo "    realm: ollama-chat   client: ollama-chat-app"
	@echo "    Google IDP redirect: https://auth.gm-global-techies-town.club/realms/ollama-chat/broker/google/endpoint"
	@echo ""
	@echo "  $(BOLD)$(CYN)── Useful commands ──────────────────────────────────────────────────$(RESET)"
	@echo "    $(DIM)make logs-ollama-chat$(RESET)   tail Ollama Chat logs"
	@echo "    $(DIM)make logs-ollama$(RESET)        tail Ollama logs"
	@echo "    $(DIM)make logs-pgadmin$(RESET)       tail pgAdmin logs"
	@echo "    $(DIM)make ollama-list$(RESET)        list loaded models"
	@echo "    $(DIM)make shell-db$(RESET)           psql prompt"
	@echo ""

# ── Open key URLs in default browser ─────────────────────────────────────────
open:
	@echo "$(BOLD)$(GRN)▶  Opening URLs in browser…$(RESET)"
	@xdg-open http://localhost:8001/docs 2>/dev/null || \
	  open http://localhost:8001/docs 2>/dev/null || \
	  echo "  $(YLW)Visit: http://localhost:8001/docs$(RESET)"

# ── Restart cloudflared (needed after adding a new tunnel route) ──────────────
restart-tunnel:
	@echo "$(BOLD)$(CYN)▶  Restarting cloudflared tunnel…$(RESET)"
	@podman restart auth-service_cloudflared_1 2>/dev/null || docker restart auth-service_cloudflared_1
	@echo "$(GRN)  Done. New routes are now active.$(RESET)"

# ── Stop + wipe all volumes (full reset) ─────────────────────────────────────
clean:
	@echo "$(BOLD)$(RED)▶  Removing containers AND volumes (full reset)…$(RESET)"
	@$(COMPOSE) down -v 2>/dev/null || true
	@echo "$(DIM)  pg_data, ollama_data, pgadmin_data, and ollama_chat_data volumes removed.$(RESET)"

# ── Auto-create .env from example if missing ──────────────────────────────────
.env:
	@echo "$(YLW)  .env not found — copying from .env.example$(RESET)"
	@cp .env.example .env
	@echo "$(YLW)  !! Edit .env: set UPI_VPA, SECRET_KEY, HYPERSWITCH_API_KEY$(RESET)"

# ── Internal: poll until API is healthy, then print done ──────────────────────
_wait:
	@printf "$(DIM)  Waiting for services"
	@for i in $$(seq 1 40); do \
	    API=$$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8001/health 2>/dev/null); \
	    OLL=$$(curl -s -o /dev/null -w "%{http_code}" http://localhost:11434 2>/dev/null); \
	    if [ "$$API" = "200" ]; then \
	        echo "$(RESET)"; \
	        echo "  $(GRN)✔  API ready$(RESET)"; \
	        if [ "$$OLL" = "200" ]; then \
	            echo "  $(GRN)✔  Ollama ready$(RESET)"; \
	        else \
	            echo "  $(YLW)⚠  Ollama still starting — run 'make logs-ollama' to check$(RESET)"; \
	        fi; \
	        break; \
	    fi; \
	    printf "$(DIM).$(RESET)"; \
	    sleep 2; \
	done

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "$(BOLD)$(CYN)  $(PROJECT) — Makefile$(RESET)"
	@echo ""
	@echo "  $(BOLD)make up$(RESET)           Start all containers (no rebuild)"
	@echo "  $(BOLD)make build$(RESET)        Build images then start (after code changes)"
	@echo "  $(BOLD)make rebuild$(RESET)      Force-rebuild from scratch (no layer cache)"
	@echo "  $(BOLD)make down$(RESET)         Stop containers (volumes kept)"
	@echo "  $(BOLD)make restart$(RESET)      Restart all containers"
	@echo "  $(BOLD)make clean$(RESET)        Stop + wipe ALL volumes (full reset)"
	@echo ""
	@echo "  $(BOLD)make logs$(RESET)         Tail all container logs"
	@echo "  $(BOLD)make logs-api$(RESET)           Tail API logs"
	@echo "  $(BOLD)make logs-db$(RESET)            Tail PostgreSQL logs"
	@echo "  $(BOLD)make logs-ollama$(RESET)        Tail Ollama logs"
	@echo "  $(BOLD)make logs-ollama-chat$(RESET)   Tail Ollama Chat logs"
	@echo "  $(BOLD)make logs-pgadmin$(RESET)       Tail pgAdmin logs"
	@echo ""
	@echo "  $(BOLD)make ps$(RESET)           Show container status"
	@echo "  $(BOLD)make urls$(RESET)         Print all service URLs"
	@echo "  $(BOLD)make open$(RESET)         Open Swagger UI in browser"
	@echo ""
	@echo "  $(BOLD)make shell-api$(RESET)    Shell into the API container"
	@echo "  $(BOLD)make shell-db$(RESET)     psql prompt inside PostgreSQL"
	@echo "  $(BOLD)make ollama-pull$(RESET)  Pull llava into Ollama container (if missing)"
	@echo "  $(BOLD)make ollama-list$(RESET)  List models available in Ollama container"
	@echo "  $(BOLD)make migrate$(RESET)      Apply migrations/init.sql to the DB"
	@echo ""
