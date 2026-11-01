# 真实模型端到端验证（后台跑；结束后把输出留在 tests\_corpus\*.log）
$ErrorActionPreference = 'Continue'
$root = 'C:\Users\Administrator\Desktop\condense'
$corpus = Join-Path $root 'tests\_corpus'
$new = Join-Path $root 'condense.py'
$old = 'C:\Users\Administrator\AppData\Local\Hermes\condense\condense.py'

function Run-One($tag, $exe, $args_) {
    $log = Join-Path $corpus "$tag.log"
    Write-Host "=== $tag ==="
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & python $exe @args_ *> $log
    $code = $LASTEXITCODE
    $sw.Stop()
    Write-Host "$tag exit=$code  用时 $([math]::Round($sw.Elapsed.TotalSeconds,1))s  -> $log"
}

# 1. 中文日志，正常 ctx
Run-One 'A_zh_ctx8192' $new @( (Join-Path $corpus 'synth_zh.log'), '--ctx', '8192', '--out', (Join-Path $corpus 'out_zh_8192.md') )
# 2. 中文日志，故意小 ctx：逼出真机上的溢出二分自救
Run-One 'B_zh_ctx2048' $new @( (Join-Path $corpus 'synth_zh.log'), '--ctx', '2048', '--limit', '12', '--out', (Join-Path $corpus 'out_zh_2048.md') )
# 3. 真实英文日志
Run-One 'C_en_ctx8192' $new @( (Join-Path $corpus 'real_agent_en.log'), '--ctx', '8192', '--out', (Join-Path $corpus 'out_en_8192.md') )
# 4. 对照组：旧的 Hermes 副本跑同一个用例
Run-One 'D_OLD_zh_ctx2048' $old @( (Join-Path $corpus 'synth_zh.log'), '--ctx', '2048', '--limit', '12', '--out', (Join-Path $corpus 'out_zh_2048_OLD.md') )

Write-Host 'ALL DONE'
