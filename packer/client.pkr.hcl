packer {
  required_version = "= 1.16.0"
  required_plugins {
    vultr = {
      version = "= 2.7.0"
      source  = "github.com/vultr/vultr"
    }
  }
}

variable "vultr_api_key" {
  type      = string
  sensitive = true
  default   = env("ODUFLOW_VULTR_API_KEY")
}

variable "region" {
  type    = string
  default = "ams"
}

variable "source_revision" {
  type    = string
  default = "working-tree"
  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.source_revision))
    error_message = "Use a nonsecret source revision or release identifier."
  }
}

locals {
  build_stamp = formatdate("YYYYMMDD-hhmmss", timestamp())
}

source "vultr" "client" {
  api_key              = var.vultr_api_key
  os_id                = 2284
  # Building Paseo from source needs far more builder CPU, memory and scratch
  # disk than installing a package did. The builder stays disposable; the
  # published snapshot keeps the same 25 GB client disk contract.
  plan_id              = "vc2-4c-8gb"
  region_id            = var.region
  instance_label       = "oduflow-image-${local.build_stamp}"
  hostname             = "oduflow-image-builder"
  snapshot_description = "oduflow-image-v1 ubuntu24.04 disk25 source-${var.source_revision} ${local.build_stamp}"
  tags                 = ["oduflow-image-builder"]
  enable_ipv6          = false
  ssh_username         = "root"
  ssh_timeout          = "15m"
  state_timeout        = "60m"
}

build {
  sources = ["source.vultr.client"]

  provisioner "shell" {
    inline = ["cloud-init status --wait", "install -d -m 0700 /opt/oduflow-image/salt"]
  }
  provisioner "file" {
    source      = "${path.root}/../salt/"
    destination = "/opt/oduflow-image/salt/"
  }
  provisioner "shell" {
    script = "${path.root}/scripts/install.sh"
  }
  provisioner "shell" {
    script = "${path.root}/scripts/clean.sh"
  }
  post-processor "manifest" {
    output     = "${path.root}/output/manifest.json"
    strip_path = true
    custom_data = {
      image_contract  = "oduflow-image-v1"
      source_revision = var.source_revision
      os_id           = "2284"
      minimum_disk_gb = "25"
    }
  }
}
