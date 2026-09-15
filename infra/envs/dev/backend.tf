# The bucket is created by infra/bootstrap. Backend blocks cannot read variables, so its name is
# spelled out here and must match that config's 'state_bucket' output.
terraform {
  backend "s3" {
    bucket       = "bfd-tfstate-533335672274"
    key          = "envs/dev/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}
