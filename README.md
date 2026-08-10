# Mega-Container Ubuntu + Windows Tools

Este é um container Docker ultra-completo baseado em **Ubuntu 24.04**, projetado para oferecer um ambiente de desenvolvimento robusto com a conveniência de comandos familiares do **Windows**.

## 🚀 Destaques

- **Base:** Ubuntu 24.04 (LTS)
- **Funções:** Mais de 309 funções utilitárias pré-carregadas.
- **Aliases Windows:** `dir`, `cls`, `ipconfig`, `tasklist`, `copy`, `move`, `del`, etc.
- **Ferramentas Inclusas:**
  - **Linguagens:** Python, Node.js, Go, Rust, C++.
  - **Rede:** nmap, tcpdump, net-tools, curl, wget.
  - **Sistema:** htop, neofetch, tmux, vim, nano.
  - **DevOps:** Git, Docker (client), rsync.

## 🛠️ Como Usar

### 1. Build do Container
```bash
docker build -t mega-container .
```

### 2. Rodar o Container
```bash
docker run -it mega-container
```

### 3. Usar as Funções
Ao entrar no container, todas as funções e aliases já estarão disponíveis. Tente digitar `dir` ou `func_1`.

## 📂 Estrutura do Projeto
- `Dockerfile`: Configuração da imagem.
- `functions.sh`: Script contendo as centenas de funções e aliases.

---
Criado automaticamente por **Manus AI**.
