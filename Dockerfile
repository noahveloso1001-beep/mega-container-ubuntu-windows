# Usar a versão mais recente do Ubuntu como base
FROM ubuntu:24.04

# Evitar prompts interativos durante a instalação
ENV DEBIAN_FRONTEND=noninteractive

# Atualizar e instalar ferramentas essenciais (Cerca de 50+ pacotes base)
RUN apt-get update && apt-get upgrade -y && apt-get install -y \
    sudo \
    curl \
    wget \
    git \
    vim \
    nano \
    zip \
    unzip \
    tar \
    gzip \
    build-essential \
    net-tools \
    iputils-ping \
    dnsutils \
    htop \
    tree \
    jq \
    python3 \
    python3-pip \
    nodejs \
    npm \
    golang \
    rustc \
    cargo \
    software-properties-common \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
    tmux \
    screen \
    zsh \
    fish \
    neofetch \
    ffmpeg \
    imagemagick \
    sqlite3 \
    nmap \
    tcpdump \
    traceroute \
    whois \
    telnet \
    openssh-client \
    rsync \
    rclone \
    && apt-get clean

# Criar um usuário padrão e dar permissões de sudo
RUN useradd -m -s /bin/bash dev && echo "dev:dev" | chpasswd && adduser dev sudo
RUN echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Copiar o script de funções para o container
COPY functions.sh /home/dev/.mega_functions.sh

# Configurar o Bash para carregar as funções e aliases "estilo Windows"
RUN echo "source /home/dev/.mega_functions.sh" >> /home/dev/.bashrc
RUN chown dev:dev /home/dev/.mega_functions.sh

# Definir o diretório de trabalho
WORKDIR /home/dev

# Mudar para o usuário dev
USER dev

# Comando padrão ao iniciar o container
CMD ["/bin/bash"]
